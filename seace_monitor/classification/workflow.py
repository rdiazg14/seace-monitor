"""Casos de uso del flujo C1/C4: proponer, consenso, aplicar, camino directo.

Los comandos reciben sus dependencias por ``ConfigClasificacion`` (la
composición vive en el entrypoint raíz). No leen env ni crean clientes por
su cuenta: ``supa_opcional`` y las funciones de vocabulario se inyectan.

Regla de oro documentada en el flujo: la IA rankea pero nunca oculta; una
discrepancia IT-interna escribe la categoría de P1 y marca ``revisar``.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from .artefactos import (
    ARTEFACTO_MAX_DIAS,
    DATA_DIR,
    cargar_artefacto,
    conteos_items,
    escribir_json,
    ruta_artefacto,
    ruta_consenso,
)
from .cola import COLA_PATH, persistir_cola_revision
from .contracts import (
    CATEGORIA_NINGUNA,
    CATEGORIAS_IT,
    RESPONSE_SCHEMA,
    RESPONSE_SCHEMA_P1,
    RESPONSE_SCHEMA_P2,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_P1,
    SYSTEM_PROMPT_P2,
)
from .cuota import (
    CUOTA_C4_PATH,
    EXIT_CUPO_C4,
    MAX_LLAMADAS_DIA_DEFAULT,
    CupoClasificacion,
    stats_tokens,
)
from .ledger import (
    LEDGER_PATH,
    aplicar_ledger,
    cargar_ledger,
    categoria_efectiva,
    categoria_propuesta_escritura,
)
from .pasada import clasificar_lote, correr_pasada
from .repository import escribir_gemini, map_confianza
from .rules import _parse_dt, anexar_verificacion_p2, aplicar_respuestas, degradar_p1, recortar
from .service import user_prompt, user_prompt_p2

BATCH_DB = 100
BATCH_P2 = 15
SEED_P2 = 20260902


@dataclass
class ConfigClasificacion:
    """Dependencias del pipeline C1/C4, armadas en la composición.

    ``clasificar`` permite inyectar un clasificador falso en pruebas; cuando
    es None se usa la llamada real a Gemini con la cuota C4 activa.
    """

    api_key: str = field(repr=False)
    url: str
    modelo: str
    supa: Any = None
    supa_opcional: Callable[[], Any | None] = lambda: None
    conectar_supa: Callable[[], Any | None] = lambda: None
    stats: dict = field(default_factory=stats_tokens)
    max_llamadas: int = MAX_LLAMADAS_DIA_DEFAULT
    data_dir: Path = DATA_DIR
    cuota_path: Path = CUOTA_C4_PATH
    ledger_path: Path = LEDGER_PATH
    cola_path: Path = COLA_PATH
    batch_p2: int = BATCH_P2
    seed_p2: int = SEED_P2
    cargar_pistas: Callable[[Any], str] | None = None
    registrar_keywords: Callable[[Any, list[dict]], tuple[int, int]] | None = None
    clasificar: Callable[..., list[dict]] | None = None
    # Transporte IA: None = el histórico Gemini; la composición puede inyectar
    # un callable con la misma firma (p.ej. clasificar_lote_openai con
    # modelo/params pre-ligados vía functools.partial) desde la config ia_*.
    transporte: Callable[..., list[dict]] | None = None
    timeout: float | None = None
    version_config: int = 0
    precio: dict | None = None

    def clasificar_lote(self, client: httpx.Client, lote: list[dict], **kw):
        if self.clasificar is not None:
            return self.clasificar(client, lote, **kw)
        extra: dict = {}
        if self.transporte is not None:
            extra["transporte"] = self.transporte
        if self.timeout is not None:
            extra["timeout"] = self.timeout
        if self.precio is not None:
            extra.update(modelo=self.modelo, precio=self.precio,
                         version_config=self.version_config)
        return clasificar_lote(
            client,
            lote,
            api_key=self.api_key,
            url=self.url,
            supa=self.supa,
            stats=self.stats,
            max_llamadas=self.max_llamadas,
            cuota_path=self.cuota_path,
            **extra,
            **kw,
        )


def correr_pasada_cfg(
    cfg: ConfigClasificacion,
    client: httpx.Client,
    filas: list[dict],
    batch: int,
    *,
    etiqueta: str,
    system_prompt: str,
    schema: dict,
    armar_prompt,
) -> tuple[dict[int, dict], set[int]]:
    def clasificar(lote: list[dict]) -> list[dict]:
        return cfg.clasificar_lote(
            client,
            lote,
            system_prompt=system_prompt,
            schema=schema,
            armar_prompt=armar_prompt,
        )

    return correr_pasada(filas, batch, etiqueta=etiqueta, clasificar=clasificar)


def _fila_aplicar_gemini(it: dict, *, artefacto: str, consenso_n: int) -> dict:
    cat = categoria_propuesta_escritura(it)
    if it.get("origen") == "desempate_ok" and isinstance(it.get("p2"), dict):
        src = it["p2"]
    else:
        src = it.get("p1") or {}
    return {
        "contrato_id": int(it["id"]),
        "categoria_it": cat,
        "senal": (src.get("senal") or None),
        "senal_fuente": (src.get("senal_fuente") or None),
        "confianza": map_confianza(src.get("confianza")),
        "consenso_n": consenso_n,
        "revisar": bool(it.get("revisar")),
        "artefacto": artefacto,
    }


def flush_upsert(supa, lote: list[dict]) -> None:
    """DEPRECATED camino directo: redirige a clasificacion_contrato."""
    if not lote:
        return
    filas = [
        {
            "contrato_id": int(p["id"]),
            "categoria_it": p["categoria_it"],
            "consenso_n": 0,
            "artefacto": "camino_directo",
        }
        for p in lote
    ]
    n, s = escribir_gemini(filas, supa=supa)
    print(f"    clasificacion gemini lote={len(lote)} escritos={n} saltados={s}",
          flush=True)


def _registrar_keywords(cfg: ConfigClasificacion, supa, items: list[dict]) -> None:
    if cfg.registrar_keywords is None:
        return
    try:
        n_n, n_i = cfg.registrar_keywords(supa, items)
        print(
            f"  keyword_candidatas nuevas={n_n} incrementadas={n_i}",
            flush=True,
        )
    except Exception as e:
        print(f"  [aviso] keyword_candidatas no se registro: {e}", flush=True)


def comando_proponer(
    cfg: ConfigClasificacion,
    *,
    filas: list[dict],
    filtro: str,
    limit: int,
    batch: int,
    incluir_ventana_cerrada: bool,
) -> int:
    cfg.stats.update(stats_tokens())
    pistas = ""
    if cfg.cargar_pistas is not None:
        pistas = cfg.cargar_pistas(cfg.supa) or ""
    if pistas:
        n_lin = pistas.count("\n")
        print(f"  pistas vocabulario P2: {n_lin} lineas", flush=True)
    por_id = {int(r["id"]): r for r in filas}
    p1_map: dict[int, dict] = {}
    sin_p1: set[int] = set()
    p2_map: dict[int, dict] = {}
    sin_p2: set[int] = set()

    try:
        with httpx.Client() as client:
            if filas:
                p1_map, sin_p1 = correr_pasada_cfg(
                    cfg,
                    client,
                    filas,
                    batch,
                    etiqueta="P1",
                    system_prompt=SYSTEM_PROMPT_P1,
                    schema=RESPONSE_SCHEMA_P1,
                    armar_prompt=user_prompt,
                )
                for cid, p in p1_map.items():
                    degradar_p1(p, por_id[cid])
                # 91197 salio Licencias en una corrida y Cloud/hosting en otra,
                # ambas con confianza alta y senal verificada; la confianza
                # declarada no predice estabilidad de CATEGORIA. Costo: ~5
                # llamadas extra sobre 1802 contratos. La confianza degradada
                # se guarda como diagnostico, no decide quien va a P2.
                ids_p2 = [
                    cid
                    for cid, p in p1_map.items()
                    if p["categoria"] != CATEGORIA_NINGUNA
                ]
                rng = random.Random(cfg.seed_p2)
                rng.shuffle(ids_p2)
                print(
                    f"  pasada 2 candidatos={len(ids_p2)} batch={cfg.batch_p2} seed={cfg.seed_p2}",
                    flush=True,
                )
                filas_p2 = [por_id[cid] for cid in ids_p2]
                if filas_p2:
                    p2_map, sin_p2 = correr_pasada_cfg(
                        cfg,
                        client,
                        filas_p2,
                        cfg.batch_p2,
                        etiqueta="P2",
                        system_prompt=SYSTEM_PROMPT_P2,
                        schema=RESPONSE_SCHEMA_P2,
                        armar_prompt=lambda lote: user_prompt_p2(
                            lote, pistas=pistas
                        ),
                    )
    except CupoClasificacion as e:
        print(f"ERROR cupo C4 (exit {EXIT_CUPO_C4}): {e}", flush=True)
        return EXIT_CUPO_C4

    items: list[dict] = []
    for row in filas:
        cid = int(row["id"])
        item = {
            "id": cid,
            "descripcion": recortar(row.get("descripcion"), 120),
            "entidad": recortar(row.get("entidad"), 80),
            "p1": None,
            "p2": None,
            "decision": "sin_respuesta",
            "origen": None,
            "en_ledger": False,
            "revisar": False,
        }
        if cid in sin_p1 or cid not in p1_map:
            items.append(item)
            continue
        p1 = p1_map[cid]
        item["p1"] = p1
        if p1["categoria"] == CATEGORIA_NINGUNA:
            item["decision"] = "no_escribir"
            item["origen"] = "ninguna"
        elif cid in sin_p2 or cid not in p2_map:
            item["decision"] = "sin_respuesta"
            item["origen"] = None
        else:
            p2 = p2_map[cid]
            anexar_verificacion_p2(p2, row)
            item["p2"] = p2
            if p1["categoria"] == p2["categoria"]:
                if p2.get("senal_verificada"):
                    item["decision"] = "escribir"
                    item["origen"] = "desempate_ok"
                else:
                    item["decision"] = "cola"
                    item["origen"] = "desempate_sin_evidencia"
            elif (
                p1["categoria"] != CATEGORIA_NINGUNA
                and p2["categoria"] != CATEGORIA_NINGUNA
            ):
                # Si ambas pasadas coinciden en que es IT y difieren solo
                # en cual de las 13, dejar NULL lo saca de Ruta del dia por
                # completo (el filtro es categoria_it OR relevancia_ia NOT
                # NULL). Ocultar es peor que etiquetar suboptimo: la regla
                # de oro es que la IA rankea pero nunca oculta. "revisar"
                # alimenta la cola de C3. Se escribe la categoria de P1.
                item["decision"] = "escribir"
                item["origen"] = "discrepa_intra_it"
                item["revisar"] = True
            else:
                item["decision"] = "cola"
                item["origen"] = "discrepa_es_it"
                item["revisar"] = True
        items.append(item)

    aplicar_ledger(items, cargar_ledger(cfg.supa_opcional(), path=cfg.ledger_path))
    counts = conteos_items(items)
    ahora = datetime.now(timezone.utc)
    payload = {
        "meta": {
            "generado_utc": ahora.isoformat(),
            "modelo": cfg.modelo,
            "version_config": cfg.version_config or None,
            "filtro": filtro,
            "limit": limit,
            "batch_p1": batch,
            "batch_p2": cfg.batch_p2,
            "universo_seleccionado": len(filas),
            "version_c": "C1.5",
            "incluir_ventana_cerrada": bool(incluir_ventana_cerrada),
            "tokens": {
                "prompt": cfg.stats["prompt"],
                "candidates": cfg.stats["candidates"],
                "total": cfg.stats["total"],
                "llamadas": cfg.stats["llamadas"],
            },
            "conteos": counts,
        },
        "items": items,
    }
    path = ruta_artefacto(ahora, data_dir=cfg.data_dir)
    escribir_json(path, payload)
    persistir_cola_revision(
        items, path, ahora, path=cfg.cola_path, supa=cfg.supa_opcional()
    )
    _registrar_keywords(cfg, cfg.supa, items)

    print("\n  id | decision | origen | p1 | p2 | descripcion", flush=True)
    for it in items:
        p1c = (it.get("p1") or {}).get("categoria") or "-"
        p2c = (it.get("p2") or {}).get("categoria") or "-"
        print(
            f"  {it['id']} | {it['decision']} | {it.get('origen') or '-'} | "
            f"{p1c} | {p2c} | {it['descripcion']}",
            flush=True,
        )
    revisar = [it for it in items if it.get("revisar")]
    if revisar:
        print("\n  REVISAR (escritos pero con discrepancia)", flush=True)
        print("  id | origen | p1 | p2 | descripcion", flush=True)
        for it in revisar:
            p1c = (it.get("p1") or {}).get("categoria") or "-"
            p2c = (it.get("p2") or {}).get("categoria") or "-"
            print(
                f"  {it['id']} | {it.get('origen') or '-'} | "
                f"{p1c} | {p2c} | {it['descripcion']}",
                flush=True,
            )
    print("\n--- resumen C1 --proponer ---", flush=True)
    print(f"  artefacto={path}", flush=True)
    print(f"  universo={len(filas)}", flush=True)
    for k, n in counts.items():
        print(f"    {k}: {n}", flush=True)
    print(
        f"  tokens prompt={cfg.stats['prompt']} "
        f"candidates={cfg.stats['candidates']} "
        f"total={cfg.stats['total']} llamadas={cfg.stats['llamadas']}",
        flush=True,
    )
    print("  no se escribio en Supabase.", flush=True)
    if counts["sin_respuesta"] > 0:
        print(
            f"ERROR: sin_respuesta={counts['sin_respuesta']} "
            "(corrida incompleta; artefacto escrito)",
            flush=True,
        )
        return 3
    return 0


def comando_consenso(cfg: ConfigClasificacion, rutas: list[str]) -> int:
    """Cruza artefactos --proponer. No llama Gemini. No escribe Supabase."""
    paths = [Path(r) for r in rutas]
    payloads: list[dict] = []
    for p in paths:
        payload, code = cargar_artefacto(p)
        if payload is None:
            return code
        payloads.append(payload)

    metas = [(p.get("meta") or {}) if isinstance(p.get("meta"), dict) else {}
             for p in payloads]
    filtros = [m.get("filtro") for m in metas]
    ventanas = [bool(m.get("incluir_ventana_cerrada")) for m in metas]
    versions = [m.get("version_c") for m in metas]

    if any(f is None for f in filtros) or len(set(filtros)) != 1:
        print(
            "ERROR: --consenso aborta (exit 6): los artefactos no tienen el "
            f"mismo filtro: { {n.name: f for n, f in zip(paths, filtros)} }",
            flush=True,
        )
        return 6
    if len(set(ventanas)) != 1:
        print(
            "ERROR: --consenso aborta (exit 6): los artefactos no tienen el "
            "mismo incluir_ventana_cerrada: "
            f"{ {n.name: v for n, v in zip(paths, ventanas)} }",
            flush=True,
        )
        return 6
    if any(v is None for v in versions) or len(set(versions)) != 1:
        print(
            "ERROR: --consenso aborta (exit 6): los artefactos no tienen el "
            f"mismo version_c: { {n.name: v for n, v in zip(paths, versions)} }",
            flush=True,
        )
        return 6
    for path, payload in zip(paths, payloads):
        if payload.get("aplicado"):
            print(
                f"ERROR: --consenso aborta (exit 6): {path.name} ya tiene "
                "bloque aplicado. No se puede cruzar un artefacto ya escrito.",
                flush=True,
            )
            return 6

    item_maps: list[dict[int, dict]] = []
    id_sets: list[set[int]] = []
    for path, payload in zip(paths, payloads):
        m: dict[int, dict] = {}
        for it in payload.get("items") or []:
            if not isinstance(it, dict):
                continue
            try:
                m[int(it["id"])] = it
            except (TypeError, ValueError, KeyError):
                continue
        item_maps.append(m)
        id_sets.append(set(m))
        print(f"  {path.name}: {len(m)} ids  version_c={payload.get('meta', {}).get('version_c')}",
              flush=True)

    union = set.union(*id_sets) if id_sets else set()
    inter = set.intersection(*id_sets) if id_sets else set()
    descartados = union - inter
    if descartados:
        print(
            f"[aviso] universos no coinciden: interseccion={len(inter)} "
            f"union={len(union)} ids_descartados={len(descartados)}",
            flush=True,
        )
        for path, s in zip(paths, id_sets):
            extra = s - inter
            if extra:
                muestra = ", ".join(str(i) for i in sorted(extra, reverse=True)[:12])
                mas = "" if len(extra) <= 12 else f" ... +{len(extra) - 12}"
                print(
                    f"  {path.name}: {len(extra)} ids fuera ({muestra}{mas})",
                    flush=True,
                )
    else:
        print(f"  universos coinciden: {len(inter)} ids", flush=True)

    n_corridas = len(paths)
    nombres = [p.name for p in paths]
    ultimo = item_maps[-1]
    orden = [cid for cid in ultimo if cid in inter]

    items: list[dict] = []
    for cid in orden:
        votos: dict[str, str] = {}
        cats: list[str] = []
        for nombre, m in zip(nombres, item_maps):
            cat = categoria_efectiva(m[cid])
            votos[nombre] = cat
            cats.append(cat)
        last_it = ultimo[cid]
        item = {
            "id": cid,
            "descripcion": last_it.get("descripcion"),
            "entidad": last_it.get("entidad"),
            "p1": last_it.get("p1"),
            "p2": last_it.get("p2"),
            "decision": "cola",
            "origen": None,
            "en_ledger": False,
            "revisar": False,
            "votos": votos,
            "n_corridas": n_corridas,
        }
        if cats and all(c == cats[0] for c in cats):
            if cats[0] != CATEGORIA_NINGUNA:
                item["decision"] = "escribir"
                item["origen"] = "consenso_unanime"
            else:
                item["decision"] = "no_escribir"
                item["origen"] = "consenso_ninguna"
        else:
            item["decision"] = "cola"
            item["origen"] = "consenso_inestable"
            item["revisar"] = True
        items.append(item)

    aplicar_ledger(items, cargar_ledger(cfg.supa_opcional(), path=cfg.ledger_path))
    # El ledger puede bajar un unanime a rechazado_previo; el conteo
    # tiene que reflejar lo que --aplicar escribiria, no el voto crudo.
    n_unanime = sum(
        1 for it in items
        if it.get("origen") == "consenso_unanime"
        and it.get("decision") == "escribir"
    )
    n_ninguna = sum(
        1 for it in items if it.get("origen") == "consenso_ninguna"
    )
    n_inestable = sum(
        1 for it in items if it.get("origen") == "consenso_inestable"
    )
    ahora = datetime.now(timezone.utc)
    counts = {
        "consenso_unanime": n_unanime,
        "consenso_ninguna": n_ninguna,
        "consenso_inestable": n_inestable,
    }
    payload = {
        "meta": {
            "generado_utc": ahora.isoformat(),
            "modo": "consenso",
            "version_c": "C1.5",
            "artefactos": nombres,
            "n_corridas": n_corridas,
            "universo_interseccion": len(inter),
            "ids_descartados": len(descartados),
            "filtro": filtros[0],
            "incluir_ventana_cerrada": ventanas[0],
            "conteos": counts,
        },
        "items": items,
    }
    path = ruta_consenso(ahora, data_dir=cfg.data_dir)
    escribir_json(path, payload)
    persistir_cola_revision(
        items, path, ahora, path=cfg.cola_path, supa=cfg.supa_opcional()
    )

    unanimes = [
        it for it in items
        if it.get("origen") == "consenso_unanime"
        and it.get("decision") == "escribir"
    ]
    inestables = [it for it in items if it.get("origen") == "consenso_inestable"]
    if unanimes:
        print("\n  consenso_unanime", flush=True)
        print("  id | categoria | descripcion", flush=True)
        for it in unanimes:
            cat = categoria_efectiva(it)
            print(
                f"  {it['id']} | {cat} | {it.get('descripcion') or ''}",
                flush=True,
            )
    if inestables:
        print("\n  consenso_inestable", flush=True)
        for it in inestables:
            print(
                f"  {it['id']} | votos={json.dumps(it.get('votos'), ensure_ascii=False)} "
                f"| {it.get('descripcion') or ''}",
                flush=True,
            )
    print("\n--- resumen C1 --consenso ---", flush=True)
    print(f"  artefacto={path}", flush=True)
    print(f"  n_corridas={n_corridas}  interseccion={len(inter)}  "
          f"descartados={len(descartados)}", flush=True)
    for k, n in counts.items():
        print(f"    {k}: {n}", flush=True)
    print("  no se escribio en Supabase.", flush=True)
    return 0


def reselect_ids(supa, ids: list[int]) -> dict[int, dict]:
    """Releer clasificacion actual de clasificacion_contrato (fase 6)."""
    out: dict[int, dict] = {}
    for i in range(0, len(ids), BATCH_DB):
        chunk = ids[i: i + BATCH_DB]
        res = (
            supa.table("clasificacion_contrato")
            .select("contrato_id,categoria_it,relevancia_ia,capa")
            .in_("contrato_id", chunk)
            .execute()
        )
        for row in res.data or []:
            out[int(row["contrato_id"])] = row
    return out


def comando_aplicar(cfg: ConfigClasificacion, ruta: str) -> int:
    # Acepta artefactos --proponer y --consenso sin cambiar la logica:
    # solo mira decision=="escribir" + categoria_propuesta_escritura.
    # Escribe clasificacion_contrato (capa=gemini); el eco copia a contratos.
    path = Path(ruta)
    if not path.is_file():
        print(f"ERROR: no existe el artefacto {path}", flush=True)
        return 1
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        print("ERROR: artefacto no es un objeto JSON", flush=True)
        return 1
    if payload.get("aplicado"):
        print(
            "ERROR: este artefacto ya se aplico (bloque aplicado). No re-aplicar.",
            flush=True,
        )
        return 5
    meta = payload.get("meta") or {}
    gen = _parse_dt(meta.get("generado_utc"))
    if gen is None:
        print("ERROR: meta.generado_utc ausente o invalido.", flush=True)
        return 1
    edad = datetime.now(timezone.utc) - gen
    if edad > timedelta(days=ARTEFACTO_MAX_DIAS):
        print(
            f"ERROR: el artefacto tiene mas de {ARTEFACTO_MAX_DIAS} dias "
            f"(generado_utc={meta.get('generado_utc')}). "
            "El estado de la BD ya no es el del SELECT original.",
            flush=True,
        )
        return 4

    consenso_n = int(meta.get("n_corridas") or 0)
    items = payload.get("items") or []
    a_escribir: list[dict] = []
    for it in items:
        if not isinstance(it, dict) or it.get("decision") != "escribir":
            continue
        cat = categoria_propuesta_escritura(it)
        if not cat:
            print(
                f"    [aviso] id={it.get('id')} decision=escribir sin categoria IT; skip",
                flush=True,
            )
            continue
        a_escribir.append(it)

    n_propuestos = len(a_escribir)
    print(
        f"  --aplicar {path.name}  decision=escribir {n_propuestos} "
        f"(clasificacion_contrato capa=gemini)",
        flush=True,
    )

    supa = cfg.conectar_supa()
    if supa is None:
        return 1

    actuales = reselect_ids(supa, [int(it["id"]) for it in a_escribir])
    pendientes: list[dict] = []
    descartados: list[int] = []
    for it in a_escribir:
        cid = int(it["id"])
        row = actuales.get(cid)
        if row is None:
            # Sin fila en clasificacion_contrato: aun sin clasificar -> escribir
            pass
        else:
            ya = row.get("categoria_it") or row.get("relevancia_ia")
            print(
                f"[skip] id={cid} ya clasificado como {ya} (capa={row.get('capa')})",
                flush=True,
            )
            descartados.append(cid)
            continue
        pendientes.append(
            _fila_aplicar_gemini(
                it, artefacto=path.name, consenso_n=consenso_n
            )
        )

    escritos_ids: list[int] = []
    try:
        for i in range(0, len(pendientes), BATCH_DB):
            lote = pendientes[i: i + BATCH_DB]
            n, s = escribir_gemini(lote, supa=supa)
            print(
                f"    clasificacion gemini lote={len(lote)} "
                f"escritos={n} saltados={s}",
                flush=True,
            )
            escritos_ids.extend(int(x["contrato_id"]) for x in lote)
    except Exception as e:
        print(f"ERROR escribiendo clasificacion_contrato: {e}", flush=True)
        return 1

    _registrar_keywords(cfg, supa, a_escribir)

    payload["aplicado"] = {
        "utc": datetime.now(timezone.utc).isoformat(),
        "escritos": escritos_ids,
        "descartados": descartados,
        "destino": "clasificacion_contrato",
    }
    escribir_json(path, payload)

    print("\n--- resumen C1 --aplicar ---", flush=True)
    print(f"  propuestos={n_propuestos}", flush=True)
    print(f"  descartados por re-select={len(descartados)}", flush=True)
    print(f"  escritos={len(escritos_ids)}", flush=True)
    print("  diff clasificacion/contratos=0", flush=True)
    return 0


def camino_directo(
    cfg: ConfigClasificacion,
    *,
    filas: list[dict],
    batch: int,
    dry_run: bool,
) -> int:
    ids_lote = {int(r["id"]) for r in filas}
    print(f"  ids en SELECT: {len(ids_lote)} (cascada: solo nulls)", flush=True)

    resultados: list[tuple[dict, str]] = []
    with httpx.Client() as client:
        n_lotes = -(-len(filas) // batch)
        for i in range(0, len(filas), batch):
            lote = filas[i: i + batch]
            num = i // batch + 1
            print(f"  lote Gemini {num}/{n_lotes} n={len(lote)}", flush=True)
            raw = cfg.clasificar_lote(client, lote)
            resultados.extend(aplicar_respuestas(lote, raw))

    cats = Counter(cat for _, cat in resultados)
    n_ninguna = cats.get(CATEGORIA_NINGUNA, 0)
    n_it = len(resultados) - n_ninguna

    print("\n  id | categoria | descripcion", flush=True)
    for row, cat in resultados:
        desc = recortar(row.get("descripcion"), 100)
        print(f"  {row['id']} | {cat} | {desc}", flush=True)

    print("\n--- resumen ---", flush=True)
    print(f"  procesados={len(resultados)}", flush=True)
    print(f"  IT={n_it}  ninguna={n_ninguna}", flush=True)
    print("  por categoria:", flush=True)
    for k, n in cats.most_common():
        print(f"    {k}: {n}", flush=True)

    a_escribir = [
        {"id": int(row["id"]), "categoria_it": cat}
        for row, cat in resultados
        if cat in CATEGORIAS_IT
    ]
    print(f"  escribirian={len(a_escribir)}  (ninguna se deja NULL)", flush=True)

    if dry_run:
        print(f"\n[dry-run] no se escribio. {len(a_escribir)} UPDATE pendientes.",
              flush=True)
        return 0

    pendiente: list[dict] = []
    for p in a_escribir:
        pendiente.append(p)
        if len(pendiente) >= BATCH_DB:
            flush_upsert(cfg.supa, pendiente)
            pendiente.clear()
    flush_upsert(cfg.supa, pendiente)
    print(f"escritos: {len(a_escribir):,}", flush=True)
    return 0
