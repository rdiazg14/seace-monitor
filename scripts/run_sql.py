#!/usr/bin/env python3
"""Ejecuta un archivo SQL completo por conexion Postgres directa.

PostgREST no ejecuta DDL ni bloques DO; las fases C2 y C3 crean tablas
y politicas RLS y necesitan este camino. Lee DATABASE_URL del .env,
ejecuta el archivo entero como un statement (no se parte por ';') y
muestra los NOTICE del servidor.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import psycopg

_ROOT = Path(__file__).resolve().parent.parent
_ENV = _ROOT / ".env"


def _cargar_env() -> None:
    if not _ENV.exists():
        return
    for line in _ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _redactar(texto: str) -> str:
    url = os.environ.get("DATABASE_URL") or ""
    if url:
        texto = texto.replace(url, "***")
    return re.sub(r"postgres(?:ql)?(?:\+[a-z]+)?://[^\s'\"<>]+", "***", texto, flags=re.I)


def _notices(diag: psycopg.errors.Diagnostic) -> None:
    sev = diag.severity or "NOTICE"
    msg = diag.message_primary or ""
    print(f"[{sev}] {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ejecuta un .sql completo contra DATABASE_URL"
    )
    ap.add_argument("ruta_sql", help="Archivo SQL a ejecutar")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Muestra el SQL y sale sin conectar",
    )
    args = ap.parse_args()

    path = Path(args.ruta_sql)
    if not path.is_file():
        print(f"ERROR: no existe el archivo {path}", flush=True)
        return 1
    sql = path.read_text(encoding="utf-8")

    if args.dry_run:
        print(sql, end="" if sql.endswith("\n") else "\n")
        return 0

    _cargar_env()
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print(
            "ERROR: falta DATABASE_URL en el entorno o en .env",
            flush=True,
        )
        return 2

    try:
        with psycopg.connect(dsn, cursor_factory=psycopg.ClientCursor) as conn:
            conn.add_notice_handler(_notices)
            try:
                cur = conn.execute(sql)
                rc = cur.rowcount
                conn.commit()
                print(f"OK rowcount={rc}", flush=True)
            except Exception:
                conn.rollback()
                raise
    except Exception as e:
        print(_redactar(f"{type(e).__name__}: {e}"), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
