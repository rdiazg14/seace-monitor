#!/usr/bin/env python3
"""
Marca el estado del pipeline en `pipeline_estado` (fila única id=1).

Lo llama el paso final del workflow diario. El front (Ruta del día) lee esta
fila para mostrar "Actualizado: <fecha/hora>" con la hora real de la última
corrida, en lugar de la fecha de hoy.
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

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def _leer_ingesta() -> tuple[int, int]:
    p = Path(__file__).parent / "data" / "ultima_ingesta.txt"
    total = 0
    nuevos = 0
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.replace(",", "").strip()
            if not v.isdigit():
                continue
            if k == "total_registros":
                total = int(v)
            elif k == "nuevos_esta_corrida":
                nuevos = int(v)
    return total, nuevos


def main() -> None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SystemExit("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY no encontrados")
    total, nuevos = _leer_ingesta()
    now = datetime.now(timezone.utc).isoformat()
    supa = create_client(SUPABASE_URL, SUPABASE_KEY)
    res = (
        supa.table("pipeline_estado")
        .upsert(
            {
                "id": 1,
                "ultima_corrida_utc": now,
                "contratos_total": total,
                "contratos_nuevos": nuevos,
                "resultado": "success",
            },
            on_conflict="id",
        )
        .execute()
    )
    print(f"pipeline_estado <- ultima_corrida_utc={now} total={total} nuevos={nuevos}")


if __name__ == "__main__":
    main()
