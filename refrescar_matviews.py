#!/usr/bin/env python3
"""
Refresca las vistas materializadas de la capa de reportes.

`dashboard_resumen` se materializó para evitar el timeout de Postgres
(ver docs/materializar_dashboard_resumen.sql). Este script la refresca
de forma CONCURRENTLY (sin bloquear lecturas) al final del pipeline diario.

pg_cron ya la refresca cada 5 minutos como red de seguridad; este paso
garantiza frescura inmediata justo después de que la ingesta modifica
estado/categoria_it/nuevos contratos.

Uso:
  python refrescar_matviews.py
"""
from __future__ import annotations

import os
from pathlib import Path

_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Materializadas a refrescar, en orden.
MATVIEWS = ["dashboard_resumen"]


def main() -> None:
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL no configurado", flush=True)
        raise SystemExit(1)

    try:
        import psycopg
    except ImportError:
        print("ERROR: psycopg no está instalado", flush=True)
        raise SystemExit(1)

    with psycopg.connect(DATABASE_URL, connect_timeout=30, sslmode="require") as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for mv in MATVIEWS:
                try:
                    cur.execute(
                        f"REFRESH MATERIALIZED VIEW CONCURRENTLY {mv}"
                    )
                    print(f"matview {mv}: refrescada OK", flush=True)
                except Exception as e:  # no romper el pipeline por un refresco
                    print(f"matview {mv}: refresco FALLÓ -> {e}", flush=True)


if __name__ == "__main__":
    main()
