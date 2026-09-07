#!/usr/bin/env python3
"""Baseline fase 5: columnas contratos + KPIs antes de migrar vistas."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
_ENV = _ROOT / ".env"
if _ENV.is_file():
    for line in _ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

import psycopg
from psycopg.rows import dict_row


def main() -> int:
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        cols = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'contratos'
            ORDER BY ordinal_position
            """
        ).fetchall()
        names = [r["column_name"] for r in cols]
        print("N_COLS", len(names))
        for n in names:
            print("COL", n)
        post = conn.execute(
            "SELECT count(*)::int AS n FROM v_contratos_estado WHERE es_postulable"
        ).fetchone()["n"]
        print("POSTULABLES", post)
        kpis = conn.execute("SELECT * FROM v_kpis_dashboard").fetchone()
        print("KPI_JSON", json.dumps(kpis, default=str, ensure_ascii=False))
        out = _ROOT / "data" / "fase5_baseline_kpis.json"
        out.write_text(
            json.dumps(
                {"postulables": post, "kpis_dashboard": kpis, "contratos_cols": names},
                default=str,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
