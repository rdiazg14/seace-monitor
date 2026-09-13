"""Historial de corridas del pipeline (append-only) en la tabla pipeline_runs.

Reemplaza los logs data/ultima_*.txt que se sobrescribían en cada corrida.
Fail-soft: un fallo al registrar nunca rompe el pipeline.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

PASO_INGESTA = "ingesta"
PASO_OCR = "ocr"
PASO_PDF = "pdf"
PASO_CAPAS = "capas"


def _jsonable(obj):
    """Convierte a JSON-serializable de forma segura (default=str para el resto)."""
    import json

    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))


def registrar_run(supa, paso: str, payload: dict, run_id: str | None = None) -> None:
    """Inserta una fila en pipeline_runs. Ignora errores (nunca lanza).

    payload va como dict (no string): supabase-py serializa el dict a jsonb.
    Se sanean los valores no JSON (datetime, etc.) a string para no fallar.
    """
    if supa is None:
        return
    try:
        supa.table("pipeline_runs").insert(
            {
                "paso": paso,
                "run_id": run_id or os.getenv("GITHUB_RUN_ID") or None,
                "ts": datetime.now(timezone.utc).isoformat(),
                "payload": _jsonable(payload),
            }
        ).execute()
    except Exception as e:
        print(f"  [warn] pipeline_runs ({paso}): {e}", flush=True)
