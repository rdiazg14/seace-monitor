#!/usr/bin/env python3
"""Sincroniza etiquetas post-fase-2 que solo estaban en contratos."""
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

ROWS = [
    (91674, "Hardware", "gemini", "c4_semanal", 3),
    (91928, "Soporte tecnico", "gemini", "c4_semanal", 3),
    (92070, "Hardware", "gemini", "c4_semanal", 3),
    (92081, "Redes/cableado", "gemini", "c4_semanal", 3),
    (92055, "Desarrollo software", "keyword", "reclasificar_diario", 0),
]


def main() -> int:
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            for cid, cat, capa, art, cn in ROWS:
                cur.execute(
                    """
                    INSERT INTO clasificacion_contrato (
                      contrato_id, categoria_it, relevancia_ia, capa,
                      consenso_n, artefacto
                    ) VALUES (%s, %s, NULL, %s, %s, %s)
                    ON CONFLICT (contrato_id) DO UPDATE SET
                      categoria_it = EXCLUDED.categoria_it,
                      capa = EXCLUDED.capa,
                      consenso_n = EXCLUDED.consenso_n,
                      artefacto = EXCLUDED.artefacto,
                      actualizado_utc = now()
                    """,
                    (cid, cat, capa, cn, art),
                )
                print(f"upsert {cid} {cat} {capa}", flush=True)
        conn.commit()
        print(f"diff={diff_clasificacion_contratos(conn)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
