#!/usr/bin/env python3
"""Valida sync clasificacion <-> contratos y smoke de escritores fase 4."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from clasificacion_capa import (  # noqa: E402
    diff_clasificacion_contratos,
    upsert_keyword,
)


def _env() -> None:
    env = _ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    _env()
    label = sys.argv[1] if len(sys.argv) > 1 else "check"
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        n = diff_clasificacion_contratos(conn)
        print(f"[{label}] diff clasificacion/contratos = {n}", flush=True)

        cur = conn.execute(
            "SELECT count(*)::int AS n FROM clasificacion_contrato "
            "WHERE capa = 'gemini' AND contrato_id = ANY(%s)",
            ([
                273, 10353, 11435, 11988, 12399, 20626, 32171, 32378, 34382, 34492,
                35576, 35751, 36445, 36973, 40586, 43667, 46129, 50908, 55367, 57244,
                57871, 57882, 58672, 59934, 63954, 65580, 65997, 66279, 67658, 68477,
                70601, 70826, 72158, 72867, 74482, 77609, 77999, 79918, 84043, 85541,
                88126, 90076, 90342, 90815, 90819, 90832, 90869, 90875, 90891, 91148,
                91197, 91221, 91321, 91342,
            ],),
        )
        print(f"[{label}] C1 capa=gemini = {cur.fetchone()['n']} (esp 54)", flush=True)

        if label == "smoke-keyword":
            row = conn.execute(
                "SELECT contrato_id, categoria_it FROM clasificacion_contrato "
                "WHERE capa='keyword' AND categoria_it IS NOT NULL "
                "ORDER BY contrato_id DESC LIMIT 1"
            ).fetchone()
            cid = int(row["contrato_id"])
            orig = row["categoria_it"]
            nueva = "Hardware" if orig != "Hardware" else "Redes/cableado"
            upsert_keyword(
                conn,
                [{"contrato_id": cid, "categoria_it": nueva}],
                artefacto="smoke_fase4",
            )
            conn.commit()
            n2 = diff_clasificacion_contratos(conn)
            c_cat = conn.execute(
                "SELECT categoria_it FROM contratos WHERE id=%s", (cid,)
            ).fetchone()["categoria_it"]
            print(f"[{label}] id={cid} contratos={c_cat} diff={n2}", flush=True)
            upsert_keyword(
                conn,
                [{"contrato_id": cid, "categoria_it": orig}],
                artefacto="smoke_fase4",
            )
            conn.commit()
            n3 = diff_clasificacion_contratos(conn)
            print(f"[{label}] revertido diff={n3}", flush=True)
            return 0 if n2 == 0 and n3 == 0 and c_cat == nueva else 1

        return 0 if n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
