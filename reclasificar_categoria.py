#!/usr/bin/env python3
"""
Backfill selectivo de categoria_it / relevancia_ia.

Reaplica las keywords de ingesta_completa.py sobre contratos que quedaron
con ambas columnas NULL. No usa Gemini. Idempotente: solo mira nulls;
reejecutar no pisa etiquetas ya pobladas.

Uso:
  python reclasificar_categoria.py --dry-run
  python reclasificar_categoria.py --dry-run --limit 200
  python reclasificar_categoria.py            # escribe (no correr sin OK)
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from supabase import create_client

from ingesta_completa import clasificar_categoria_it, clasificar_relevancia_ia

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
    """categoria_it IS NULL AND relevancia_ia IS NULL. limit=0 → todos."""
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
    """Solo id + columnas que ahora tienen valor. No manda nulls."""
    p: dict = {"id": int(row["id"])}
    if cat:
        p["categoria_it"] = cat
    if ia:
        p["relevancia_ia"] = ia
    return p


def flush_upsert(supa, lote: list[dict]) -> None:
    if not lote:
        return
    supa.table("contratos").upsert(lote, on_conflict="id").execute()
    print(f"    upsert lote {len(lote)} filas OK", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Backfill categoria_it / relevancia_ia (solo nulls, keywords)"
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Clasifica y muestra; no escribe")
    ap.add_argument("--limit", type=int, default=0,
                    help="Tope de filas a leer (0 = todas las null)")
    args = ap.parse_args()

    print("=" * 60, flush=True)
    print("Backfill categoria_it / relevancia_ia (keywords)", flush=True)
    print(f"  dry-run={args.dry_run}  limit={args.limit or 'all'}", flush=True)
    print("=" * 60, flush=True)

    supa = init_supabase()
    if supa is None:
        return 1

    print("SELECT categoria_it IS NULL AND relevancia_ia IS NULL...",
          flush=True)
    filas = paginar_nulls(supa, args.limit)
    n_sel = len(filas)
    n_pobladas = sum(
        1 for r in filas
        if r.get("categoria_it") or r.get("relevancia_ia")
    )
    print(f"  candidatos={n_sel:,}  ya_poblados_en_lote={n_pobladas}",
          flush=True)
    if n_pobladas:
        print("ERROR: el SELECT trajo filas con etiqueta; aborto.",
              flush=True)
        return 1

    now = datetime.now(timezone.utc)
    cambios: list[tuple[dict, str | None, str | None]] = []
    n_vig_ventana = 0
    n_vig_ventana_hit = 0
    cats = Counter()
    ias = Counter()

    for row in filas:
        api = fila_api(row)
        cat = clasificar_categoria_it(api)
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
            cats[cat] += 1
        if ia:
            ias[ia] += 1
        if vig:
            n_vig_ventana_hit += 1

    print(f"\n  cambiarían={len(cambios):,}  "
          f"siguen_null={n_sel - len(cambios):,}", flush=True)
    print(f"  vigente+ventana en lote={n_vig_ventana:,}  "
          f"de ellos reclasifican={n_vig_ventana_hit:,}  "
          f"siguen_null={n_vig_ventana - n_vig_ventana_hit:,}", flush=True)
    if cats:
        print("  categoria_it nueva:", flush=True)
        for k, n in cats.most_common():
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
        print(f"\n[dry-run] no se escribió. {len(cambios):,} UPDATE pendientes.",
              flush=True)
        return 0

    pendiente: list[dict] = []
    for row, cat, ia in cambios:
        pendiente.append(payload_update(row, cat, ia))
        if len(pendiente) >= BATCH_DB:
            flush_upsert(supa, pendiente)
            pendiente.clear()
    flush_upsert(supa, pendiente)
    print(f"escritos: {len(cambios):,}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
