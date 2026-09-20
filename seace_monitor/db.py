"""Acceso directo a Postgres (psycopg) centralizado.

Reemplaza el patrón repetido de ``psycopg.connect(DATABASE_URL)`` en los scripts
que escriben directo a la BD (para no heredar el ``statement_timeout`` de 8 s de
PostgREST). El DSN se lee de ``DATABASE_URL``; los parámetros extra
(``connect_timeout``, ``sslmode``, etc.) se pasan tal cual cuando hacen falta.
"""
from __future__ import annotations

import psycopg

from .config import obtener_strip


def connect(dsn: str | None = None, **kwargs):
    """Abre una conexión psycopg.

    Sin ``dsn`` usa ``DATABASE_URL`` del entorno. Lanza ``SystemExit`` si falta.
    """
    dsn = (dsn or obtener_strip("DATABASE_URL")).strip()
    if not dsn:
        raise SystemExit("ERROR: DATABASE_URL no configurado")
    return psycopg.connect(dsn, **kwargs)
