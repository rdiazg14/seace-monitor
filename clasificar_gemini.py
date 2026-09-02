#!/usr/bin/env python3
"""
Fase B: clasifica categoria_it con Gemini sobre lo que keywords dejo en NULL.

Cascada: SELECT siempre categoria_it IS NULL AND relevancia_ia IS NULL.
No pisa etiquetas de keywords. No toca relevancia_ia. No escribe
flash_ocr_cuota.json.

HERRAMIENTA MANUAL. temperature:0 no es determinista: el dry-run del 1 sep
dijo 1 y la escritura fueron 3 (2 FP Hardware revertidos). Revisar el
SELECT a mano antes de confiar. No meter en pipeline.yml sin Arquitectura C.

Uso:
    python clasificar_gemini.py --dry-run --limit 30 --filtro vigentes
    python clasificar_gemini.py --dry-run --filtro vigentes
    python clasificar_gemini.py --filtro vigentes            # escribe
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx
from supabase import create_client

_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_FLASH = (
    os.getenv("GEMINI_FLASH_MODEL", "").strip() or "gemini-3.7-flash"
)
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_FLASH}:generateContent"
)
GEMINI_BACKOFF = (2.0, 4.0, 8.0, 16.0, 32.0, 64.0)
BATCH_DB = 100
PAGE_DB = 1_000
TIMEOUT_S = 120.0

CATEGORIAS_IT = [
    "Firma digital",
    "IA/analytics",
    "Ciberseguridad",
    "Cloud/hosting",
    "Microsoft",
    "Oracle",
    "Base de datos/ERP",
    "Desarrollo software",
    "Licencias",
    "Soporte tecnico",
    "Redes/cableado",
    "Correo electronico",
    "Hardware",
]
CATEGORIA_NINGUNA = "ninguna"
ENUM_CATEGORIA = CATEGORIAS_IT + [CATEGORIA_NINGUNA]

RESPONSE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "categoria": {"type": "string", "enum": ENUM_CATEGORIA},
        },
        "required": ["id", "categoria"],
    },
}

# Una linea por categoria, derivada de IT_CATS (ingesta_completa.py).
DEF_CATEGORIAS = """\
- Firma digital: firma digital, certificado digital/electronico, token criptografico.
- IA/analytics: inteligencia artificial, LLM/GPT/Copilot, analytics, BI, big data, ML.
- Ciberseguridad: ciberseguridad, seguridad informatica/de la informacion, firewall, pentest.
- Cloud/hosting: nube publica, cloud computing, hosting, servidor virtual, AWS, Google Cloud.
- Microsoft: Microsoft 365, Office 365, SharePoint, Exchange, Windows Server.
- Oracle: Oracle Database, Oracle EBS, PeopleSoft.
- Base de datos/ERP: motores SQL, data warehouse, SAP, ERP.
- Desarrollo software: creacion/implementacion de sistemas, aplicativos web o moviles, software a medida.
- Licencias: licencia, licenciamiento o suscripcion de software.
- Soporte tecnico: soporte tecnico, mantenimiento de software/sistemas, mesa de ayuda.
- Redes/cableado: red de datos, cableado estructurado, switch, router, wifi, fibra optica.
- Correo electronico: correo o mensajeria electronica.
- Hardware: compra de equipos de computo (PC, laptop, impresora, monitor, disco, RAM, scanner); no electrodomesticos ni estabilizadores de oficina.
- ninguna: el objeto NO es tecnologia de la informacion.
"""

SYSTEM_PROMPT = (
    "Eres un clasificador de contratos publicos peruanos para una "
    "empresa de TI (ENERTRONIC: IA, cloud, desarrollo de software, servicios TI). "
    "Clasificas cada contrato en UNA de 13 categorias IT, o 'ninguna' si NO es "
    "un contrato de tecnologia de la informacion.\n"
    "REGLA CRITICA: clasifica por el OBJETO real del contrato (que se compra o "
    "contrata), NO por el area que lo solicita. Un area de TI/Informatica que "
    "compra aire acondicionado, mobiliario, estabilizadores o pide personal "
    "administrativo NO es un contrato IT -> 'ninguna'. Un area no-TI que compra "
    "desarrollo de software SI es IT.\n"
    "Personal: locar o contratar a una persona (bachiller, ingeniero, locador, "
    "practicante, 'servicio de un profesional') NO es Desarrollo software aunque "
    "el titulo sea de sistemas/informatica. Desarrollo software es crear o "
    "implementar un sistema/aplicativo/software, no alquilar un profesional.\n"
    "Hardware es SOLO equipos de computo (PC, laptop, impresora, monitor, disco, "
    "RAM, scanner, UPS de datacenter). Estabilizadores de tension de oficina, "
    "repuestos genericos, electrodomesticos o aire acondicionado -> 'ninguna'.\n"
    "Definicion breve de cada categoria:\n"
    f"{DEF_CATEGORIAS}"
    "Ante la duda entre una categoria IT y 'ninguna', prefiere 'ninguna' si el "
    "objeto no es claramente tecnologia (mejor no clasificar que clasificar mal).\n"
    "Responde solo el JSON array del schema, un objeto por contrato de entrada."
)

COLS = (
    "id,descripcion,descripcion_contrato,objeto,entidad,"
    "nom_area_usuaria,items_json,categoria_it,relevancia_ia,"
    "estado,fecha_fin_cotizacion"
)


def init_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY no encontrados",
              flush=True)
        return None
    try:
        client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        print("[supabase] cliente inicializado OK", flush=True)
        return client
    except Exception as e:
        print(f"ERROR: no se pudo conectar a Supabase: {e}", flush=True)
        return None


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


def pasa_filtro(row: dict, filtro: str, now: datetime) -> bool:
    est = row.get("estado") or ""
    fin = _parse_dt(row.get("fecha_fin_cotizacion"))
    vigente_ok = est == "Vigente" and (fin is None or fin >= now)
    if filtro == "todos":
        return True
    if filtro == "vigentes":
        return vigente_ok
    if filtro == "evaluacion":
        return vigente_ok or est == "En Evaluacion" or est == "En Evaluación"
    return False


def cubsos(row: dict) -> str:
    items = row.get("items_json") or []
    if not isinstance(items, list):
        return ""
    names: list[str] = []
    for it in items[:8]:
        if not isinstance(it, dict):
            continue
        n = (it.get("nom_cubso") or it.get("descripcion") or "").strip()
        if n:
            names.append(n[:80])
    return "; ".join(names)


def recortar(s, n: int = 220) -> str:
    t = " ".join(str(s or "").split())
    return t if len(t) <= n else t[: n - 1] + "..."


def paginar_nulls(supa, filtro: str, limit: int) -> list[dict]:
    """categoria_it IS NULL AND relevancia_ia IS NULL + filtro de universo."""
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    offset = 0
    while True:
        q = (
            supa.table("contratos")
            .select(COLS)
            .is_("categoria_it", "null")
            .is_("relevancia_ia", "null")
            .order("id", desc=True)
        )
        if filtro == "vigentes":
            q = q.eq("estado", "Vigente")
        elif filtro == "evaluacion":
            q = q.in_("estado", ["Vigente", "En Evaluación", "En Evaluacion"])
        res = q.range(offset, offset + PAGE_DB - 1).execute()
        batch = res.data or []
        for row in batch:
            if not pasa_filtro(row, filtro, now):
                continue
            out.append(row)
            if limit and len(out) >= limit:
                break
        print(f"  leidos {len(out):,}", flush=True)
        if len(batch) < PAGE_DB:
            break
        if limit and len(out) >= limit:
            break
        offset += PAGE_DB
    return out[:limit] if limit else out


def extract_gemini_text(body: dict) -> str:
    parts = (
        (body.get("candidates") or [{}])[0]
        .get("content", {})
        .get("parts") or []
    )
    textos = [p.get("text") or "" for p in parts if not p.get("thought")]
    return "".join(textos).strip()


def parse_array(text: str) -> list[dict]:
    s = text.strip().replace("```json", "").replace("```", "").strip()
    raw = json.loads(s)
    if isinstance(raw, dict):
        for v in raw.values():
            if isinstance(v, list):
                raw = v
                break
    if not isinstance(raw, list):
        raise RuntimeError(f"gemini no devolvio array (type={type(raw).__name__})")
    return raw


def user_prompt(lote: list[dict]) -> str:
    lineas = [
        "Clasifica estos contratos. Devuelve un JSON array con un objeto "
        "{id, categoria} por cada id de entrada.",
        "",
    ]
    for i, row in enumerate(lote, 1):
        lineas.append(f"{i}. id={row['id']}")
        lineas.append(f"   descripcion: {recortar(row.get('descripcion'), 400)}")
        lineas.append(f"   objeto: {recortar(row.get('objeto'), 80)}")
        lineas.append(f"   entidad: {recortar(row.get('entidad'), 120)}")
        lineas.append(f"   area_usuaria: {recortar(row.get('nom_area_usuaria'), 160)}")
        lineas.append(f"   cubso: {recortar(cubsos(row), 240)}")
        lineas.append("")
    return "\n".join(lineas)


def clasificar_lote(client: httpx.Client, lote: list[dict]) -> list[dict]:
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt(lote)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "thinkingConfig": {"thinkingLevel": "LOW"},
            "temperature": 0,
            "maxOutputTokens": 8192,
        },
    }
    waits = [0.0] + list(GEMINI_BACKOFF)
    last_err: Exception | None = None
    for attempt, wait in enumerate(waits):
        if wait:
            print(f"    [gemini backoff {wait:.0f}s attempt={attempt}]", flush=True)
            time.sleep(wait)
        try:
            r = client.post(
                GEMINI_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": GEMINI_API_KEY,
                },
                json=payload,
                timeout=TIMEOUT_S,
            )
            if r.status_code == 429:
                last_err = RuntimeError(f"429 {r.text[:200]}")
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        extra = min(float(retry_after), 120.0)
                        print(f"    [429 Retry-After {extra:.0f}s]", flush=True)
                        time.sleep(extra)
                    except ValueError:
                        pass
                continue
            r.raise_for_status()
            text = extract_gemini_text(r.json())
            if not text:
                raise RuntimeError("gemini vacio")
            return parse_array(text)
        except httpx.HTTPStatusError as e:
            last_err = e
            code = e.response.status_code if e.response is not None else 0
            if e.response is not None and e.response.status_code in (429, 500, 503):
                print(f"    [retry {attempt}] HTTP {code}", flush=True)
                continue
            raise
        except Exception as e:
            last_err = e
            print(f"    [retry {attempt}] {e}", flush=True)
    raise RuntimeError(f"clasificar_lote fallo: {last_err}")


def aplicar_respuestas(
    lote: list[dict],
    raw: list[dict],
) -> list[tuple[dict, str]]:
    """(row, categoria_enum) incluyendo 'ninguna'. Ignora ids ajenos."""
    by_id = {int(r["id"]): r for r in lote}
    vistos: set[int] = set()
    out: list[tuple[dict, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            cid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if cid not in by_id or cid in vistos:
            continue
        cat = str(item.get("categoria") or "").strip()
        if cat not in ENUM_CATEGORIA:
            print(f"    [aviso] id={cid} categoria fuera de enum: {cat!r} -> ninguna",
                  flush=True)
            cat = CATEGORIA_NINGUNA
        vistos.add(cid)
        out.append((by_id[cid], cat))
    for cid, row in by_id.items():
        if cid not in vistos:
            print(f"    [aviso] id={cid} ausente en respuesta Gemini -> ninguna",
                  flush=True)
            out.append((row, CATEGORIA_NINGUNA))
    return out


def flush_upsert(supa, lote: list[dict]) -> None:
    if not lote:
        return
    supa.table("contratos").upsert(lote, on_conflict="id").execute()
    print(f"    upsert lote {len(lote)} filas OK", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fase B: categoria_it con Gemini (solo nulls de keywords)"
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Clasifica y muestra; no escribe")
    ap.add_argument("--limit", type=int, default=0,
                    help="Tope de contratos (0 = todos del filtro)")
    ap.add_argument(
        "--filtro",
        choices=("vigentes", "evaluacion", "todos"),
        default="vigentes",
        help="Universo (default vigentes)",
    )
    ap.add_argument("--batch", type=int, default=30,
                    help="Tamano de lote Gemini (default 30)")
    args = ap.parse_args()
    if args.batch <= 0:
        print("ERROR: --batch debe ser > 0", flush=True)
        return 1

    print("=" * 60, flush=True)
    print("Fase B -- clasificar categoria_it (Gemini)", flush=True)
    print(
        f"  dry-run={args.dry_run}  filtro={args.filtro}  "
        f"limit={args.limit or 'all'}  batch={args.batch}  "
        f"modelo={GEMINI_FLASH}",
        flush=True,
    )
    print("=" * 60, flush=True)

    if not GEMINI_API_KEY:
        print("ERROR: GEMINI_API_KEY no encontrado", flush=True)
        return 1
    supa = init_supabase()
    if supa is None:
        return 1

    print("SELECT categoria_it IS NULL AND relevancia_ia IS NULL...",
          flush=True)
    filas = paginar_nulls(supa, args.filtro, args.limit)
    n_sel = len(filas)
    n_pobladas = sum(
        1 for r in filas
        if r.get("categoria_it") or r.get("relevancia_ia")
    )
    print(f"  candidatos={n_sel:,}  ya_poblados_en_lote={n_pobladas}",
          flush=True)
    if n_pobladas:
        print("ERROR: el SELECT trajo filas con etiqueta; aborto (cascada).",
              flush=True)
        return 1
    if not filas:
        print("Nada que clasificar.", flush=True)
        return 0

    ids_lote = {int(r["id"]) for r in filas}
    print(f"  ids en SELECT: {len(ids_lote)} (cascada: solo nulls)", flush=True)

    resultados: list[tuple[dict, str]] = []
    with httpx.Client() as client:
        n_lotes = -(-len(filas) // args.batch)
        for i in range(0, len(filas), args.batch):
            lote = filas[i: i + args.batch]
            num = i // args.batch + 1
            print(f"  lote Gemini {num}/{n_lotes} n={len(lote)}", flush=True)
            raw = clasificar_lote(client, lote)
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

    if args.dry_run:
        print(f"\n[dry-run] no se escribio. {len(a_escribir)} UPDATE pendientes.",
              flush=True)
        return 0

    pendiente: list[dict] = []
    for p in a_escribir:
        pendiente.append(p)
        if len(pendiente) >= BATCH_DB:
            flush_upsert(supa, pendiente)
            pendiente.clear()
    flush_upsert(supa, pendiente)
    print(f"escritos: {len(a_escribir):,}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
