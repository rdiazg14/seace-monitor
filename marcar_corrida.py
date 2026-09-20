#!/usr/bin/env python3
"""
Marca el estado del pipeline en `pipeline_estado` (fila única id=1).

Lo llama el paso final del workflow diario. El front (Ruta del día) lee esta
fila para mostrar "Actualizado: <fecha/hora>" con la hora real de la última
corrida, en lugar de la fecha de hoy.
"""
from __future__ import annotations

from seace_monitor.config import cargar_env
from seace_monitor.supabase_client import crear_cliente

from datetime import datetime, timezone
from pathlib import Path

cargar_env()


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
    total, nuevos = _leer_ingesta()
    now = datetime.now(timezone.utc).isoformat()
    supa = crear_cliente()
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
