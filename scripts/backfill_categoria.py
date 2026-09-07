#!/usr/bin/env python3
"""C2 fase 4: backfill de categoria_it con la cascada de it_keywords.

No pisa los 54 ids escritos por C1 (consenso + verificacion de senal)
ni IDS_PROTEGIDOS (90331, Gemini camino directo). No toca relevancia_ia. No reutiliza reclasificar_categoria.py: ese solo
mira ambas columnas NULL y no puede desetiquetar.

    uv run python scripts/backfill_categoria.py --proponer
    uv run python scripts/backfill_categoria.py --aplicar data/backfill_c2_YYYYMMDD-HHMMSS.json

--aplicar escribe con psycopg UPDATE (categoria_it = %s, NULL explicito).
No usa upsert de supabase-py: PostgREST/supabase-py no garantizan persistir
JSON null y un upsert parcial podria omitir la columna en vez de desetiquetar.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from ingesta_completa import _contiene, _norm, _texto_contrato  # noqa: E402
from clasificacion_capa import (  # noqa: E402
    diff_clasificacion_contratos,
    upsert_keyword,
)

_ENV = _ROOT / ".env"
DATA_DIR = _ROOT / "data"
ARTEFACTO_MAX_DIAS = 7
BATCH_DB = 100

# 54 ids que C1 --aplicar escribio el 2026-09-03
# (data/consenso_it_20260903-043502.json -> aplicado.escritos).
# La cola data/revisar_categoria.json NO es suficiente: guarda discrepancias
# e inestables, no el lote completo escrito. Se une igual por defensa.
IDS_C1_HARDCODE: frozenset[int] = frozenset({
    273, 10353, 11435, 11988, 12399, 20626, 32171, 32378, 34382, 34492,
    35576, 35751, 36445, 36973, 40586, 43667, 46129, 50908, 55367, 57244,
    57871, 57882, 58672, 59934, 63954, 65580, 65997, 66279, 67658, 68477,
    70601, 70826, 72158, 72867, 74482, 77609, 77999, 79918, 84043, 85541,
    88126, 90076, 90342, 90815, 90819, 90832, 90869, 90875, 90891, 91148,
    91197, 91221, 91321, 91342,
})

# 90331: etiqueta puesta por clasificar_gemini.py (camino directo, 1 sep).
# No paso por consenso y esta pendiente de revision humana, pero un backfill
# de keywords no la puede borrar: un substring que no matchea no es evidencia
# de que la etiqueta este mal. Va a la cola, no a NULL.
IDS_PROTEGIDOS: frozenset[int] = frozenset({90331})

TESTIGOS_FASE4: tuple[int, ...] = (
    91505, 92055, 91928, 92040, 91674, 91637, 92167,
    92056, 92070, 92081, 91688, 92082, 91583, 91936,
)


def _contiene_plural(texto_norm: str, kw: str) -> bool:
    """s/es opcional en cada palabra. \\b palabrae?s? \\b ..."""
    kn = _norm(kw)
    words = kn.split()
    if not words:
        return False
    parts = [re.escape(w) + r"e?s?" for w in words]
    return bool(re.search(r"\b" + r"\s+".join(parts) + r"\b", texto_norm))


def _match_kw(texto_norm: str, d: dict) -> bool:
    kw = d["keyword"]
    if d.get("tolera_plural"):
        return _contiene_plural(texto_norm, kw)
    return _contiene(texto_norm, kw, bool(d.get("limite_palabra")))


def _es_postulable(row: dict, ahora: datetime) -> bool:
    if row.get("estado") != "Vigente":
        return False
    ini = row.get("fecha_ini_cotizacion")
    fin = row.get("fecha_fin_cotizacion")
    if ini is not None and ini > ahora:
        return False
    if fin is not None and fin < ahora:
        return False
    return True


def _cargar_env() -> None:
    if not _ENV.exists():
        return
    for line in _ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _dsn() -> str:
    _cargar_env()
    dsn = (os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        print("ERROR: falta DATABASE_URL en el entorno o en .env", flush=True)
        sys.exit(2)
    return dsn


def _redactar(texto: str) -> str:
    url = os.environ.get("DATABASE_URL") or ""
    if url:
        texto = texto.replace(url, "***")
    return texto


def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _cat(valor) -> str | None:
    if valor is None:
        return None
    s = str(valor).strip()
    return s or None


def _titulo(row: dict, n: int = 90) -> str:
    t = row.get("descripcion") or row.get("descripcion_contrato") or row.get("objeto") or ""
    t = " ".join(str(t).split())
    return t[:n]


def _fila_api(row: dict) -> dict:
    return {
        "desObjetoContrato": row.get("descripcion") or "",
        "desContratacion": row.get("descripcion_contrato") or "",
        "nomObjetoContrato": row.get("objeto") or "",
        "nomEntidad": row.get("entidad") or "",
    }


def cargar_ids_c1() -> tuple[set[int], dict]:
    """Union: hardcode 54 + aplicado.escritos + cola con escrita no null."""
    desde_consenso: set[int] = set()
    for path in sorted(DATA_DIR.glob("consenso_it_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"[aviso] no se pudo leer {path.name}: {e}", flush=True)
            continue
        if not isinstance(payload, dict):
            continue
        escritos = (payload.get("aplicado") or {}).get("escritos") or []
        for x in escritos:
            try:
                desde_consenso.add(int(x))
            except (TypeError, ValueError):
                continue

    desde_cola: set[int] = set()
    cola_path = DATA_DIR / "revisar_categoria.json"
    if cola_path.is_file():
        try:
            cola = json.loads(cola_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"[aviso] no se pudo leer {cola_path.name}: {e}", flush=True)
            cola = None
        items = (cola or {}).get("items") if isinstance(cola, dict) else None
        for it in items or []:
            if not isinstance(it, dict):
                continue
            if it.get("escrita") in (None, ""):
                continue
            try:
                desde_cola.add(int(it["id"]))
            except (TypeError, ValueError, KeyError):
                continue

    ids = (set(IDS_C1_HARDCODE) | desde_consenso | desde_cola) - set(IDS_PROTEGIDOS)
    detalle = {
        "camino": (
            "union: hardcode 54 (consenso_it_20260903-043502 aplicado.escritos) "
            "+ data/consenso_it_*.json aplicado.escritos "
            "+ data/revisar_categoria.json items con escrita no null. "
            "La cola sola no cubre los 54 escritos."
        ),
        "n_hardcode": len(IDS_C1_HARDCODE),
        "n_consenso_aplicado": len(desde_consenso),
        "n_cola_escrita": len(desde_cola),
        "n_union": len(ids),
        "hardcode_menos_archivos": sorted(
            IDS_C1_HARDCODE - desde_consenso - desde_cola
        ),
        "archivos_menos_hardcode": sorted(
            (desde_consenso | desde_cola) - IDS_C1_HARDCODE - IDS_PROTEGIDOS
        ),
    }
    return ids, detalle


def ids_excluidos_universo() -> tuple[set[int], set[int], dict]:
    """C1 escritos + IDS_PROTEGIDOS. No se clasifican ni se escriben."""
    ids_c1, detalle = cargar_ids_c1()
    return ids_c1 | set(IDS_PROTEGIDOS), ids_c1, detalle


def cargar_cascada(conn) -> list[tuple[str, list[dict]]]:
    filas = conn.execute(
        """
        SELECT categoria, keyword, tipo, limite_palabra, prioridad, tolera_plural
        FROM it_keywords
        WHERE activa
        ORDER BY prioridad, id
        """
    ).fetchall()
    grupos: dict[str, list[dict]] = {}
    for f in filas:
        grupos.setdefault(f["categoria"], []).append({
            "keyword": f["keyword"],
            "tipo": f.get("tipo") or "incluye",
            "limite_palabra": bool(f.get("limite_palabra")),
            "tolera_plural": bool(f.get("tolera_plural")),
        })
    if len(grupos) < 14:
        print(
            f"ERROR: it_keywords activa tiene {len(grupos)} categorias "
            "(se esperan 14, con Telemetria/OT). Aborto.",
            flush=True,
        )
        sys.exit(1)
    return list(grupos.items())


def clasificar_con_kw(
    api: dict,
    cats: list[tuple[str, list[dict]]],
) -> tuple[str | None, str | None]:
    """(categoria, keyword incluye que gano)."""
    t = _texto_contrato(api)
    for cat, kws in cats:
        if any(_match_kw(t, d) for d in kws if d.get("tipo") == "excluye"):
            continue
        for d in kws:
            if d.get("tipo") == "excluye":
                continue
            if _match_kw(t, d):
                return cat, d["keyword"]
    return None, None


def keyword_desetiqueta(
    api: dict,
    cats: list[tuple[str, list[dict]]],
    cat_antes: str,
) -> str | None:
    """Exclude de la categoria previa que disparo, si existe."""
    t = _texto_contrato(api)
    for cat, kws in cats:
        if cat != cat_antes:
            continue
        for d in kws:
            if d.get("tipo") != "excluye":
                continue
            if _match_kw(t, d):
                return d["keyword"]
        return None
    return None


def _conectar():
    return psycopg.connect(_dsn(), row_factory=dict_row)


def comando_proponer() -> int:
    ids_excluidos, ids_c1, detalle_c1 = ids_excluidos_universo()
    print("C2 fase 4 --proponer (no escribe contratos)", flush=True)
    print(f"  ids_c1 union={len(ids_c1)}  {detalle_c1['camino']}", flush=True)
    print(
        f"  hardcode={detalle_c1['n_hardcode']} "
        f"consenso_aplicado={detalle_c1['n_consenso_aplicado']} "
        f"cola_escrita={detalle_c1['n_cola_escrita']} "
        f"protegidos={sorted(IDS_PROTEGIDOS)}",
        flush=True,
    )
    extra = detalle_c1["archivos_menos_hardcode"]
    if extra:
        print(f"  [aviso] ids en archivos fuera del hardcode: {extra}", flush=True)
    faltan = detalle_c1["hardcode_menos_archivos"]
    if faltan:
        print(
            f"  [aviso] ids hardcode no hallados en archivos: {faltan}",
            flush=True,
        )

    ahora = datetime.now(timezone.utc)
    with _conectar() as conn:
        cats = cargar_cascada(conn)
        n_kw = conn.execute(
            "SELECT count(*) AS n FROM it_keywords WHERE activa"
        ).fetchone()["n"]
        print(
            f"  cascada it_keywords: {n_kw} activas, {len(cats)} categorias",
            flush=True,
        )
        contratos = conn.execute(
            """
            SELECT id, descripcion, descripcion_contrato, objeto, entidad,
                   categoria_it, estado, fecha_ini_cotizacion, fecha_fin_cotizacion
            FROM contratos
            ORDER BY id
            """
        ).fetchall()

    n_c1_en_corpus = 0
    n_prot_en_corpus = 0
    por_accion: Counter[str] = Counter()
    por_alta: Counter[str] = Counter()
    por_cambio: Counter[tuple[str, str]] = Counter()
    por_deset: Counter[str] = Counter()
    items: list[dict] = []
    ids_en_items: set[int] = set()
    testigos: dict[int, dict] = {}
    altas_postulables: list[dict] = []

    for row in contratos:
        cid = int(row["id"])
        if cid in ids_excluidos:
            if cid in ids_c1:
                n_c1_en_corpus += 1
            if cid in IDS_PROTEGIDOS:
                n_prot_en_corpus += 1
            if cid in TESTIGOS_FASE4:
                testigos[cid] = {
                    "id": cid,
                    "titulo": _titulo(row),
                    "antes": _cat(row.get("categoria_it")),
                    "despues": None,
                    "accion": "excluido_c1_o_protegido",
                    "keyword_que_gano": None,
                }
            continue
        api = _fila_api(row)
        despues, kw_g = clasificar_con_kw(api, cats)
        antes = _cat(row.get("categoria_it"))
        if antes is None and despues is not None:
            accion = "alta"
            por_alta[despues] += 1
            kw = kw_g
        elif antes is not None and despues is not None and antes != despues:
            accion = "cambio"
            por_cambio[(antes, despues)] += 1
            kw = kw_g
        elif antes is not None and despues is None:
            accion = "desetiqueta"
            por_deset[antes] += 1
            kw = keyword_desetiqueta(api, cats, antes)
        else:
            accion = "sin_cambio"
            kw = kw_g if despues is not None else None
        por_accion[accion] += 1
        rec = {
            "id": cid,
            "titulo": _titulo(row),
            "antes": antes,
            "despues": despues,
            "accion": accion,
            "keyword_que_gano": kw,
        }
        if cid in TESTIGOS_FASE4:
            testigos[cid] = rec
        if accion == "alta" and _es_postulable(row, ahora):
            altas_postulables.append(rec)
        if accion == "sin_cambio":
            continue
        ids_en_items.add(cid)
        items.append(rec)

    choque_c1 = sorted(ids_en_items & ids_c1)
    choque_prot = sorted(ids_en_items & set(IDS_PROTEGIDOS))
    if choque_c1 or choque_prot:
        print(
            f"ERROR: ids excluidos en items C1={choque_c1} "
            f"protegidos={choque_prot}",
            flush=True,
        )
        return 1

    meta = {
        "generado_utc": ahora.isoformat(),
        "n_contratos": len(contratos),
        "n_universo": len(contratos) - n_c1_en_corpus - n_prot_en_corpus,
        "n_c1_excluidos": n_c1_en_corpus,
        "n_protegidos_excluidos": n_prot_en_corpus,
        "ids_c1_excluidos": sorted(ids_c1),
        "ids_protegidos": sorted(IDS_PROTEGIDOS),
        "c1_detalle": detalle_c1,
        "n_keywords_activas": n_kw,
        "n_categorias": len(cats),
        "por_accion": {
            "alta": por_accion.get("alta", 0),
            "cambio": por_accion.get("cambio", 0),
            "desetiqueta": por_accion.get("desetiqueta", 0),
            "sin_cambio": por_accion.get("sin_cambio", 0),
        },
        "n_items": len(items),
        "alta_por_categoria": dict(por_alta.most_common()),
        "cambio_por_par": {
            f"{a} -> {b}": n for (a, b), n in por_cambio.most_common()
        },
        "desetiqueta_por_categoria": dict(por_deset.most_common()),
    }
    payload = {"meta": meta, "items": items}
    stamp = ahora.strftime("%Y%m%d-%H%M%S")
    path = DATA_DIR / f"backfill_c2_{stamp}.json"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("\n--- meta --proponer ---", flush=True)
    print(json.dumps(meta, ensure_ascii=False, indent=2), flush=True)
    print(f"\nartefacto: {path}", flush=True)
    print(f"ids C1 en items: {len(choque_c1)} (debe ser 0)", flush=True)
    print(
        f"ids protegidos en items: {len(choque_prot)} (debe ser 0) "
        f"ids={sorted(IDS_PROTEGIDOS)}",
        flush=True,
    )
    en_items_85729 = next((it for it in items if it["id"] == 85729), None)
    print(
        f"85729 en items: {en_items_85729}",
        flush=True,
    )

    cambios = [it for it in items if it["accion"] == "cambio"]
    desets = [it for it in items if it["accion"] == "desetiqueta"]
    cambios.sort(key=lambda x: -x["id"])
    desets.sort(key=lambda x: -x["id"])
    print("\n--- 15 cambio (id desc) ---", flush=True)
    for it in cambios[:15]:
        print(
            f"  {it['id']}\t{it['antes']} -> {it['despues']}\t"
            f"kw={it['keyword_que_gano']!r}\t{it['titulo']}",
            flush=True,
        )
    print("\n--- 15 desetiqueta (id desc) ---", flush=True)
    for it in desets[:15]:
        print(
            f"  {it['id']}\t{it['antes']} -> NULL\t"
            f"kw={it['keyword_que_gano']!r}\t{it['titulo']}",
            flush=True,
        )

    altas_postulables.sort(key=lambda x: -x["id"])
    print(
        f"\n--- altas postulables (vigente + ventana abierta): "
        f"{len(altas_postulables)} ---",
        flush=True,
    )
    for it in altas_postulables:
        print(
            f"  {it['id']}\t{it['despues']}\t"
            f"kw={it['keyword_que_gano']!r}\t{it['titulo']}",
            flush=True,
        )

    print("\n--- 14 testigos fase 4 ---", flush=True)
    for cid in TESTIGOS_FASE4:
        it = testigos.get(cid)
        if not it:
            print(f"  {cid}\tNO ESTA EN CORPUS", flush=True)
            continue
        print(
            f"  {cid}\t{it['accion']}\t"
            f"{it['antes']!r} -> {it['despues']!r}\t"
            f"kw={it['keyword_que_gano']!r}\t{it['titulo']}",
            flush=True,
        )
    return 0


def _reselect(conn, ids: list[int]) -> dict[int, str | None]:
    out: dict[int, str | None] = {}
    for i in range(0, len(ids), BATCH_DB):
        chunk = ids[i: i + BATCH_DB]
        rows = conn.execute(
            "SELECT id, categoria_it FROM contratos WHERE id = ANY(%s)",
            (chunk,),
        ).fetchall()
        for r in rows:
            out[int(r["id"])] = _cat(r.get("categoria_it"))
    return out


def _flush_update(conn, lote: list[dict]) -> None:
    """Escribe capa 3 (keyword). El eco copia a contratos.categoria_it."""
    if not lote:
        return
    filas = [
        {
            "contrato_id": int(p["id"]),
            "categoria_it": p.get("categoria_it"),
            # relevancia_ia no la toca el backfill C2; conservar vigente.
        }
        for p in lote
    ]
    # Marca ausencia de relevancia_ia key → upsert_keyword conserva la previa.
    escritos, saltados = upsert_keyword(
        conn, filas, artefacto="backfill_categoria"
    )
    print(
        f"    clasificacion keyword lote={len(lote)} "
        f"escritos={escritos} saltados_protegidos={saltados}",
        flush=True,
    )


def comando_aplicar(ruta: str) -> int:
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

    _ids_excluidos, ids_c1, _detalle = ids_excluidos_universo()
    items = payload.get("items") or []
    a_escribir: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if it.get("accion") in (None, "sin_cambio"):
            continue
        try:
            cid = int(it["id"])
        except (TypeError, ValueError, KeyError):
            print(
                f"    [aviso] item sin id valido; skip {repr(it)[:200]}",
                flush=True,
            )
            continue
        a_escribir.append({
            "id": cid,
            "categoria_it": _cat(it.get("despues")),
            "antes": _cat(it.get("antes")),
            "accion": it.get("accion"),
        })

    ids_lote = [p["id"] for p in a_escribir]
    choque_c1 = sorted(set(ids_lote) & ids_c1)
    choque_prot = sorted(set(ids_lote) & set(IDS_PROTEGIDOS))
    if choque_c1:
        print(
            "ERROR: ids de C1 en el lote a escribir. ABORTO, no se escribio nada. "
            f"ids={choque_c1}",
            flush=True,
        )
        return 1
    if choque_prot:
        print(
            "ERROR: ids protegidos en el lote a escribir. ABORTO, no se escribio nada. "
            f"ids={choque_prot}",
            flush=True,
        )
        return 1

    print(
        f"  --aplicar {path.name}  accion!=sin_cambio {len(a_escribir)} "
        f"(escribe clasificacion_contrato capa=keyword)",
        flush=True,
    )

    with _conectar() as conn:
        conn.autocommit = False
        actuales = _reselect(conn, ids_lote)
        pendientes: list[dict] = []
        descartados: list[int] = []
        pendientes_accion: dict[int, str] = {}
        for p in a_escribir:
            cid = p["id"]
            if cid not in actuales:
                print(f"[skip] id={cid} no aparece en el re-SELECT", flush=True)
                descartados.append(cid)
                continue
            if actuales[cid] != p["antes"]:
                print(
                    f"[skip] id={cid} categoria_it actual={actuales[cid]!r} "
                    f"!= antes={p['antes']!r}",
                    flush=True,
                )
                descartados.append(cid)
                continue
            pendientes.append({
                "id": cid,
                "categoria_it": p["categoria_it"],
            })
            pendientes_accion[cid] = str(p.get("accion") or "")

        escritos_ids: list[int] = []
        lote: list[dict] = []
        try:
            for p in pendientes:
                lote.append(p)
                if len(lote) >= BATCH_DB:
                    _flush_update(conn, lote)
                    escritos_ids.extend(int(x["id"]) for x in lote)
                    lote.clear()
            if lote:
                _flush_update(conn, lote)
                escritos_ids.extend(int(x["id"]) for x in lote)
            n_diff = diff_clasificacion_contratos(conn)
            if n_diff != 0:
                conn.rollback()
                print(
                    f"ERROR: tras aplicar, diff clasificacion vs contratos={n_diff}. "
                    "Rollback.",
                    flush=True,
                )
                return 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    payload["aplicado"] = {
        "utc": datetime.now(timezone.utc).isoformat(),
        "escritos": escritos_ids,
        "descartados": descartados,
        "destino": "clasificacion_contrato",
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("\n--- resumen C2 --aplicar ---", flush=True)
    print(f"  propuestos={len(a_escribir)}", flush=True)
    print(f"  descartados por re-select={len(descartados)}", flush=True)
    print(f"  escritos={len(escritos_ids)}", flush=True)
    print("  diff clasificacion/contratos=0", flush=True)
    por_acc = Counter(
        pendientes_accion[i] for i in escritos_ids if i in pendientes_accion
    )
    print("  por accion:", flush=True)
    for k, n in por_acc.most_common():
        print(f"    {k}: {n}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="C2 fase 4: backfill categoria_it desde it_keywords"
    )
    ap.add_argument(
        "--proponer",
        action="store_true",
        help="Clasifica todo el corpus y escribe artefacto JSON; no escribe BD",
    )
    ap.add_argument(
        "--aplicar",
        metavar="RUTA",
        default=None,
        help="Aplica un artefacto --proponer (UPDATE categoria_it, NULL explicito)",
    )
    args = ap.parse_args()
    if bool(args.proponer) == bool(args.aplicar):
        print("ERROR: indica --proponer o --aplicar RUTA (uno solo)", flush=True)
        return 2
    try:
        if args.aplicar:
            print("=" * 60, flush=True)
            print("C2 fase 4 --aplicar artefacto", flush=True)
            print("=" * 60, flush=True)
            return comando_aplicar(args.aplicar)
        print("=" * 60, flush=True)
        print("C2 fase 4 --proponer", flush=True)
        print("=" * 60, flush=True)
        return comando_proponer()
    except Exception as e:
        print(_redactar(f"ERROR {type(e).__name__}: {e}"), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
