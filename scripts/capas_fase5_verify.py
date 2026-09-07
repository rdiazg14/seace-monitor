#!/usr/bin/env python3
"""Compara KPIs post fase 5 vs baseline."""
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

BASE = _ROOT / "data" / "fase5_baseline_kpis.json"


def main() -> int:
    base = json.loads(BASE.read_text(encoding="utf-8"))
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        # columnas v_contratos == contratos
        c_cols = [
            r["column_name"]
            for r in conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='public' AND table_name='contratos'
                ORDER BY ordinal_position
                """
            )
        ]
        v_cols = [
            r["column_name"]
            for r in conn.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='public' AND table_name='v_contratos'
                ORDER BY ordinal_position
                """
            )
        ]
        print("cols_contratos", len(c_cols))
        print("cols_v_contratos", len(v_cols))
        if c_cols != v_cols:
            print("COL_MISMATCH")
            for a, b in zip(c_cols, v_cols):
                if a != b:
                    print(" !=", a, b)
            only_c = set(c_cols) - set(v_cols)
            only_v = set(v_cols) - set(c_cols)
            print("only_contratos", only_c)
            print("only_v", only_v)
            return 1
        print("cols_match=1")

        post = conn.execute(
            "SELECT count(*)::int AS n FROM v_contratos_estado WHERE es_postulable"
        ).fetchone()["n"]
        kpis = conn.execute("SELECT * FROM v_kpis_dashboard").fetchone()
        # normalizar jsonb via json
        kpis_n = json.loads(json.dumps(kpis, default=str))
        base_k = json.loads(json.dumps(base["kpis_dashboard"], default=str))

        print("POSTULABLES_ANTES", base["postulables"])
        print("POSTULABLES_DESPUES", post)
        if post != base["postulables"]:
            print("FAIL postulables moved")
            return 1
        if kpis_n != base_k:
            print("FAIL kpis_dashboard moved")
            for k in sorted(set(kpis_n) | set(base_k)):
                if kpis_n.get(k) != base_k.get(k):
                    print(f"  {k}: antes={base_k.get(k)!r} despues={kpis_n.get(k)!r}")
            return 1
        print("kpis_dashboard_match=1")

        # spot-check conversion view exists
        n = conn.execute("SELECT count(*)::int AS n FROM v_kpis_conversion").fetchone()["n"]
        print("v_kpis_conversion_rows", n)
        # sample buscar
        row = conn.execute(
            "SELECT id, categoria_it FROM buscar_contratos(%s) LIMIT 1",
            ("software",),
        ).fetchone()
        print("buscar_sample", row)
    print("OK fase5 verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
