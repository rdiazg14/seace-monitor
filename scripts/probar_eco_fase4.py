#!/usr/bin/env python3
"""Prueba puntual del trigger de eco (capas fase 4)."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

_ROOT = Path(__file__).resolve().parent.parent
_ENV = _ROOT / ".env"


def _env() -> None:
    if not _ENV.exists():
        return
    for line in _ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    _env()
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT trigger_name, action_timing, event_manipulation
                FROM information_schema.triggers
                WHERE event_object_table = 'clasificacion_contrato'
                ORDER BY 1, 3
                """
            )
            print("triggers:", [dict(r) for r in cur.fetchall()])
            cur.execute(
                "SELECT nombre FROM migraciones_datos WHERE nombre = 'capas_fase4_eco'"
            )
            print("marker:", cur.fetchone())

            cur.execute(
                """
                SELECT cl.contrato_id, cl.categoria_it, cl.relevancia_ia, cl.capa,
                       c.categoria_it AS c_cat
                FROM clasificacion_contrato cl
                JOIN contratos c ON c.id = cl.contrato_id
                WHERE cl.capa = 'keyword' AND cl.categoria_it IS NOT NULL
                ORDER BY cl.contrato_id DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if not row:
                print("ERROR: sin fila keyword de prueba")
                return 1
            print("testigo:", dict(row))
            cid = int(row["contrato_id"])
            cat_orig = row["categoria_it"]
            nueva = "Hardware" if cat_orig != "Hardware" else "Redes/cableado"

            cur.execute(
                "UPDATE clasificacion_contrato SET categoria_it = %s "
                "WHERE contrato_id = %s",
                (nueva, cid),
            )
            cur.execute(
                "SELECT categoria_it FROM contratos WHERE id = %s", (cid,)
            )
            after = cur.fetchone()
            print("despues eco contratos:", dict(after))
            print("eco_ok:", after["categoria_it"] == nueva)

            cur.execute(
                "UPDATE clasificacion_contrato SET categoria_it = %s "
                "WHERE contrato_id = %s",
                (cat_orig, cid),
            )
            cur.execute(
                "SELECT categoria_it FROM contratos WHERE id = %s", (cid,)
            )
            rev = cur.fetchone()
            print(
                "revertido:",
                rev["categoria_it"],
                "match_orig:",
                rev["categoria_it"] == cat_orig,
            )
            conn.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
