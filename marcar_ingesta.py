#!/usr/bin/env python3
"""
Marca la última consulta a la LISTA de SEACE en `pipeline_estado.ultima_ingesta_utc`.

Lo llama el paso posterior a la ingesta, tanto en el pipeline diario como en la
detección temprana (cada 2 h). El front (Ruta del día) usa esta marca para mostrar
"Última consulta a SEACE", distinta de la corrida diaria completa.
"""
from __future__ import annotations

from datetime import datetime, timezone

from seace_monitor.config import cargar_env
from seace_monitor.supabase_client import crear_cliente

cargar_env()


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    supa = crear_cliente()
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
