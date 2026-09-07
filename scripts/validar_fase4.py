#!/usr/bin/env python3
"""Validaciones fase 4 post-pipeline / post-ingesta."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from clasificacion_capa import diff_clasificacion_contratos  # noqa: E402

env = _ROOT / ".env"
for line in env.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

IDS_C1 = [
    273, 10353, 11435, 11988, 12399, 20626, 32171, 32378, 34382, 34492,
    35576, 35751, 36445, 36973, 40586, 43667, 46129, 50908, 55367, 57244,
    57871, 57882, 58672, 59934, 63954, 65580, 65997, 66279, 67658, 68477,
    70601, 70826, 72158, 72867, 74482, 77609, 77999, 79918, 84043, 85541,
    88126, 90076, 90342, 90815, 90819, 90832, 90869, 90875, 90891, 91148,
    91197, 91221, 91321, 91342,
]


def main() -> int:
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        diff = diff_clasificacion_contratos(conn)
        print(f"diff={diff}")
        n_null_capa = conn.execute(
            "SELECT count(*)::int AS n FROM clasificacion_contrato WHERE capa IS NULL"
        ).fetchone()["n"]
        print(f"capa_null={n_null_capa}")
        c1 = conn.execute(
            "SELECT count(*)::int AS n FROM clasificacion_contrato "
            "WHERE capa='gemini' AND contrato_id = ANY(%s)",
            (IDS_C1,),
        ).fetchone()["n"]
        print(f"c1_gemini={c1}")
        recientes = conn.execute(
            """
            SELECT contrato_id, categoria_it, capa, artefacto, actualizado_utc
            FROM clasificacion_contrato
            WHERE actualizado_utc > now() - interval '6 hours'
            ORDER BY actualizado_utc DESC
            LIMIT 15
            """
        ).fetchall()
        print(f"actualizados_6h={len(recientes)}")
        for r in recientes:
            print(
                f"  {r['contrato_id']} capa={r['capa']} "
                f"cat={r['categoria_it']} art={r['artefacto']}"
            )
        # contratos con etiqueta sin fila clasificacion
        huecos = conn.execute(
            """
            SELECT count(*)::int AS n
            FROM contratos c
            LEFT JOIN clasificacion_contrato cl ON cl.contrato_id = c.id
            WHERE (c.categoria_it IS NOT NULL OR c.relevancia_ia IS NOT NULL)
              AND cl.contrato_id IS NULL
            """
        ).fetchone()["n"]
        print(f"contratos_con_etiqueta_sin_capa3={huecos}")
    return 0 if diff == 0 and n_null_capa == 0 and c1 == 54 and huecos == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
