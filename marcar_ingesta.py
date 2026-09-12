#!/usr/bin/env python3
"""
Marca la última consulta a la LISTA de SEACE en `pipeline_estado.ultima_ingesta_utc`.

Lo llama el paso posterior a la ingesta, tanto en el pipeline diario como en la
detección temprana (cada 2 h). El front (Ruta del día) usa esta marca para mostrar
"Última consulta a SEACE", distinta de la corrida diaria completa.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from supabase import create_client

_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")


def main() -> None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SystemExit("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY no encontrados")
    now = datetime.now(timezone.utc).isoformat()
    supa = create_client(SUPABASE_URL, SUPABASE_KEY)
    # UPDATE (no upsert): la fila id=1 ya existe (bootstrap). Así no pisamos
    # ultima_corrida_utc ni chocamos con su NOT NULL al hacer un insert parcial.
    res = (
        supa.table("pipeline_estado")
        .update({"ultima_ingesta_utc": now})
        .eq("id", 1)
        .execute()
    )
    print(f"pipeline_estado <- ultima_ingesta_utc={now} filas={len(res.data or [])}")


if __name__ == "__main__":
    main()
