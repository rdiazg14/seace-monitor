#!/usr/bin/env python3
"""
Sincroniza contrato_items desde contratos.items_json (idempotente).

La tabla se pobló una sola vez en la fase 3 (capas_fase3_items.sql) y nadie
la alimenta desde entonces: los contratos nuevos escriben items_json pero no
la tabla, así que el JOIN con cubso_catalogo queda parcial.

Este paso reconstruye contrato_items como espejo de items_json:
  1. DELETE de toda la tabla.
  2. INSERT desde items_json con el MISMO mapeo de la fase 3
     (contrato_id, item_nro 1-based, cod_cubso, nom_cubso, descripcion,
      cantidad, unidad, distrito).

Idempotente: correrlo N veces deja el mismo estado. Atómico: una sola
transacción. Pensado para correr como paso `continue-on-error` tras
enriquecer_detalle.py; si falla, items_json ya quedó guardado (fuente de
verdad) y el pipeline no se cae.

Uso:
  python sincronizar_items.py
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import psycopg

_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

# Mismo mapeo que docs/capas_fase3_items.sql. Espejo exacto de items_json.
SQL_DELETE = "DELETE FROM contrato_items"

SQL_INSERT = """
    INSERT INTO contrato_items (
        contrato_id, item_nro, cod_cubso, nom_cubso, descripcion,
        cantidad, unidad, distrito
    )
    SELECT
        c.id,
        ord.ordinality::int AS item_nro,
        nullif(ord.elem->>'cod_cubso', ''),
        nullif(ord.elem->>'nom_cubso', ''),
        nullif(ord.elem->>'descripcion', ''),
        CASE
          WHEN ord.elem->>'cantidad' IS NULL OR ord.elem->>'cantidad' = ''
            THEN NULL
          ELSE (ord.elem->>'cantidad')::numeric
        END,
        nullif(ord.elem->>'unidad', ''),
        nullif(ord.elem->>'distrito', '')
    FROM contratos c
    CROSS JOIN LATERAL jsonb_array_elements(c.items_json)
        WITH ORDINALITY AS ord(elem, ordinality)
    WHERE c.items_json IS NOT NULL
      AND jsonb_typeof(c.items_json) = 'array'
      AND jsonb_array_length(c.items_json) > 0
"""


def main() -> int:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("ERROR: falta DATABASE_URL", flush=True)
        return 2

    t0 = time.perf_counter()
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            n_del = conn.execute(SQL_DELETE).rowcount
            n_ins = conn.execute(SQL_INSERT).rowcount
        filas = conn.execute(
            "SELECT count(*)::int FROM contrato_items"
        ).fetchone()[0]
        ctrs = conn.execute(
            "SELECT count(DISTINCT contrato_id)::int FROM contrato_items"
        ).fetchone()[0]
        desync = conn.execute(
            """
            SELECT count(*)::int
            FROM contratos c
            WHERE jsonb_typeof(c.items_json) = 'array'
              AND jsonb_array_length(c.items_json) > 0
              AND NOT EXISTS (
                SELECT 1 FROM contrato_items ci WHERE ci.contrato_id = c.id
              )
            """
        ).fetchone()[0]
    dt = time.perf_counter() - t0

    print(
        f"[items] borrados={n_del} insertados={n_ins} "
        f"filas={filas} contratos={ctrs} desync_restante={desync} "
        f"en {dt:.2f}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
