#!/usr/bin/env python3
"""
Reclasificacion diaria por keywords (C4, gratis, cero tokens).

Carga it_keywords (misma cascada que backfill_categoria / ingesta) y etiqueta
contratos Vigente / En Evaluacion que siguen con categoria_it y relevancia_ia
en NULL. Nunca desetiqueta.

Motivo: la ingesta solo clasifica ids nuevos. Cada keyword que agregamos deja
un goteo hasta que alguien re-evalua (caso 92056: 'tablet' valida, quedo NULL).

Uso:
  python reclasificar_categoria.py --dry-run
  python reclasificar_categoria.py --dry-run --limit 200
  python reclasificar_categoria.py            # escribe
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from supabase import create_client

from clasificacion_capa import anunciar_backend_capa3, escribir_keyword
from ingesta_completa import (
    cargar_keywords,
    clasificar_categoria_it,
    clasificar_relevancia_ia,
)

_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
BATCH_DB = 100
PAGE_DB = 1_000
COLS = (
    "id,descripcion,descripcion_contrato,objeto,entidad,"
    "categoria_it,relevancia_ia,estado,fecha_fin_cotizacion"
)
ESTADOS_ACCIONABLES = ("Vigente", "En Evaluación", "En Evaluacion")

# Columna BD → clave API que espera clasificar_* (preparar_fila_db inverso).
_API_DESDE_BD = (
    ("descripcion", "desObjetoContrato"),
    ("descripcion_contrato", "desContratacion"),
    ("objeto", "nomObjetoContrato"),
    ("entidad", "nomEntidad"),
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


def fila_api(row: dict) -> dict:
    """Arma el dict API desde columnas BD. None → '' (evita str(None)='None')."""
    out: dict = {}
    for col, clave in _API_DESDE_BD:
        val = row.get(col)
        out[clave] = val if val is not None else ""
    return out


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


def ventana_abierta(row: dict, now: datetime) -> bool:
    dt = _parse_dt(row.get("fecha_fin_cotizacion"))
    if dt is None:
        return False
    return dt > now


def paginar_nulls(supa, limit: int) -> list[dict]:
    """NULL en ambas columnas, solo Vigente / En Evaluacion. limit=0 → todos."""
    out: list[dict] = []
    offset = 0
    while True:
        take = PAGE_DB if not limit else min(PAGE_DB, limit - len(out))
        if take <= 0:
            break
        res = (
            supa.table("contratos")
            .select(COLS)
            .is_("categoria_it", "null")
            .is_("relevancia_ia", "null")
            .in_("estado", list(ESTADOS_ACCIONABLES))
            .order("id")
            .range(offset, offset + take - 1)
            .execute()
        )
        batch = res.data or []
        out.extend(batch)
        print(f"  leídos {len(out):,}", flush=True)
        if len(batch) < take:
            break
        offset += take
        if limit and len(out) >= limit:
            break
    return out[:limit] if limit else out


def payload_update(row: dict, cat: str | None, ia: str | None) -> dict:
    """Fila para clasificacion_contrato (capa=keyword)."""
    p: dict = {"contrato_id": int(row["id"])}
    if cat is not None:
        p["categoria_it"] = cat
    if ia is not None:
        p["relevancia_ia"] = ia
    # Si solo hay una etiqueta, la otra key no va → upsert_keyword conserva
    # la previa (o NULL en insert).
    if cat is None and ia is None:
        p["categoria_it"] = None
        p["relevancia_ia"] = None
    return p


def flush_upsert(supa, lote: list[dict]) -> None:
    """Escribe clasificacion_contrato; el eco actualiza contratos."""
    if not lote:
        return
    n, s = escribir_keyword(lote, artefacto="reclasificar_diario", supa=supa)
    print(
        f"    clasificacion keyword lote={len(lote)} "
        f"escritos={n} saltados={s}",
        flush=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Reclasificar NULL Vigente/En Evaluacion con it_keywords"
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Clasifica y muestra; no escribe")
    ap.add_argument("--limit", type=int, default=0,
                    help="Tope de filas a leer (0 = todas las null del filtro)")
    args = ap.parse_args()

    t0 = time.perf_counter()
    print("=" * 60, flush=True)
    print("Reclasificar categoria (it_keywords, C4 diario)", flush=True)
    print(f"  dry-run={args.dry_run}  limit={args.limit or 'all'}", flush=True)
    print(f"  estados={list(ESTADOS_ACCIONABLES)}", flush=True)
    print("=" * 60, flush=True)

    # Caso 92056: keyword 'tablet' valida en it_keywords, quedo NULL porque
    # la ingesta solo clasifica ids nuevos. Este paso cierra ese goteo.
    # No reescribimos reclasificar desde cero: el script ya era el lugar
    # correcto; solo le faltaba la tabla (antes usaba IT_CATS hardcoded).

    supa = init_supabase()
    if supa is None:
        return 1
    anunciar_backend_capa3(supa=supa)

    cats = cargar_keywords(supa)
    if not cats:
        print("ERROR: it_keywords vacia o inaccesible; aborto.", flush=True)
        return 1
    n_kw = sum(len(kws) for _, kws in cats)
    print(f"  cascada it_keywords: {len(cats)} categorias, {n_kw} keywords",
          flush=True)

    print(
        "SELECT NULL+NULL AND estado IN Vigente/En Evaluacion...",
        flush=True,
    )
    filas = paginar_nulls(supa, args.limit)
    n_sel = len(filas)
    n_pobladas = sum(
        1 for r in filas
        if r.get("categoria_it") or r.get("relevancia_ia")
    )
    print(f"  evaluados={n_sel:,}  ya_poblados_en_lote={n_pobladas}",
          flush=True)
    if n_pobladas:
        print("ERROR: el SELECT trajo filas con etiqueta; aborto.",
              flush=True)
        return 1

    now = datetime.now(timezone.utc)
    cambios: list[tuple[dict, str | None, str | None]] = []
    n_vig_ventana = 0
    n_vig_ventana_hit = 0
    cats_c = Counter()
    ias = Counter()

    for row in filas:
        api = fila_api(row)
        cat = clasificar_categoria_it(api, cats)
        ia = clasificar_relevancia_ia(api)
        vig = (
            (row.get("estado") or "") == "Vigente"
            and ventana_abierta(row, now)
        )
        if vig:
            n_vig_ventana += 1
        if not cat and not ia:
            continue
        cambios.append((row, cat, ia))
        if cat:
            cats_c[cat] += 1
        if ia:
            ias[ia] += 1
        if vig:
            n_vig_ventana_hit += 1

    print(f"\n  etiquetarian={len(cambios):,}  "
          f"siguen_null={n_sel - len(cambios):,}", flush=True)
    print(f"  vigente+ventana en lote={n_vig_ventana:,}  "
          f"de ellos reclasifican={n_vig_ventana_hit:,}  "
          f"siguen_null={n_vig_ventana - n_vig_ventana_hit:,}", flush=True)
    if cats_c:
        print("  categoria_it nueva:", flush=True)
        for k, n in cats_c.most_common():
            print(f"    {k}: {n:,}", flush=True)
    if ias:
        print("  relevancia_ia nueva:", flush=True)
        for k, n in ias.most_common():
            print(f"    {k}: {n:,}", flush=True)

    print("\n  id | categoria_it_nueva | relevancia_ia_nueva | descripcion",
          flush=True)
    for row, cat, ia in cambios:
        desc = (row.get("descripcion") or "").replace("\n", " ")[:120]
        print(
            f"  {row['id']} | {cat or '-'} | {ia or '-'} | {desc}",
            flush=True,
        )

    if args.dry_run:
        dur = time.perf_counter() - t0
        print(
            f"\n[dry-run] no se escribió. evaluados={n_sel:,} "
            f"etiquetarian={len(cambios):,} duracion={dur:.1f}s",
            flush=True,
        )
        return 0

    pendiente: list[dict] = []
    for row, cat, ia in cambios:
        pendiente.append(payload_update(row, cat, ia))
        if len(pendiente) >= BATCH_DB:
            flush_upsert(supa, pendiente)
            pendiente.clear()
    flush_upsert(supa, pendiente)
    dur = time.perf_counter() - t0
    print(
        f"evaluados={n_sel:,} etiquetados={len(cambios):,} "
        f"duracion={dur:.1f}s destino=clasificacion_contrato",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
