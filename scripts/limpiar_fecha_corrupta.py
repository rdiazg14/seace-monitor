#!/usr/bin/env python3
"""Limpieza idempotente de fechas de cotización corruptas (año absurdo).

Detecta y fija en NULL `fecha_fin_cotizacion` (y `fecha_ini_cotizacion`) de
contratos con año fuera de rango (>= now+3 o < 2000). La guardia de
`parsear_fecha` en ingesta/refresco ya evita NUEVAS corrupciones; este script
repara las residuales que quedaron grabadas antes de la guardia.

Uso:
  uv run python scripts/limpiar_fecha_corrupta.py --dry-run
  uv run python scripts/limpiar_fecha_corrupta.py
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import psycopg

_ROOT = Path(__file__).resolve().parent.parent
_ENV = _ROOT / ".env"


def cargar_env() -> str:
    if _ENV.exists():
        for line in _ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    dsn = (os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        print("ERROR: DATABASE_URL no encontrado", file=sys.stderr)
        sys.exit(2)
    return dsn


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    dsn = cargar_env()
    conn = psycopg.connect(dsn, autocommit=False)
    conn.execute("set statement_timeout = '30s'")

    hoy = datetime.now().year
    # "Claramente corrupto" = año > 2030 (p. ej. 2052/2611/4202), según lo
    # documentado en el cierre 12-13 sep. Los años 2027-2029 son cierres
    # tempranos con ventana larga (reales, no corruptos): NO se tocan aquí.
    limite = 2031

    filas = conn.execute(
        """
        select id, estado, fecha_ini_cotizacion, fecha_fin_cotizacion
        from contratos
        where (fecha_fin_cotizacion is not null and
               (extract(year from fecha_fin_cotizacion) >= %s
                or extract(year from fecha_fin_cotizacion) < 2000))
           or (fecha_ini_cotizacion is not null and
               (extract(year from fecha_ini_cotizacion) >= %s
                or extract(year from fecha_ini_cotizacion) < 2000))
        order by id
        """,
        (limite, limite),
    ).fetchall()

    print(f"limite año corrupto: >= {limite} o < 2000")
    print(f"filas corruptas: {len(filas)}")
    for r in filas:
        print(f"  id={r[0]} estado={r[1]!r} ini={r[2]} fin={r[3]}")

    if args.dry_run or not filas:
        if args.dry_run:
            print("DRY-RUN: no se escribió nada.")
        conn.close()
        return

    corregidas = 0
    for r in filas:
        cid = int(r[0])
        ini, fin = r[2], r[3]
        if fin is not None and (
            fin.year >= limite or fin.year < 2000
        ):
            conn.execute(
                "update contratos set fecha_fin_cotizacion = null "
                "where id = %s and fecha_fin_cotizacion = %s",
                (cid, fin),
            )
            corregidas += 1
            print(f"  -> id={cid} fecha_fin NULL")
        if ini is not None and (
            ini.year >= limite or ini.year < 2000
        ):
            conn.execute(
                "update contratos set fecha_ini_cotizacion = null "
                "where id = %s and fecha_ini_cotizacion = %s",
                (cid, ini),
            )
            corregidas += 1
            print(f"  -> id={cid} fecha_ini NULL")

    conn.commit()
    conn.close()
    print(f"corregidas: {corregidas}")


if __name__ == "__main__":
    main()
