"""Logging estructurado del pipeline (append-only) a la tabla pipeline_runs.

Módulo canónico. El archivo plano `pipeline_log.py` en la raíz es solo un shim
de retrocompatibilidad que re-exporta desde acá.

Reemplaza los logs data/ultima_*.txt que se sobrescribían en cada corrida.
Fail-soft: un fallo al registrar nunca rompe el pipeline.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

PASO_INGESTA = "ingesta"
PASO_OCR = "ocr"
PASO_PDF = "pdf"
PASO_CAPAS = "capas"
PASO_CHUNKING = "chunking"
PASO_EMBEDDING = "embedding"
PASO_CONTENEDORES = "contenedores"


def _jsonable(obj):
    """Convierte a JSON-serializable de forma segura (default=str para el resto)."""
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


def registrar_evento(
    supa,
    contrato_id: int,
    etapa: str,
    *,
    n_chunks_pdf: int | None = None,
    n_chunks_api: int | None = None,
    chars_tdr: int | None = None,
    tokens_est: int | None = None,
    costo_usd: float | None = None,
    tipo_extraccion: str | None = None,
    chunk_version: str | None = None,
    run_id: str | None = None,
    detalle: dict | None = None,
) -> None:
    """Inserta una fila en proceso_evento (seguimiento por contrato). Fail-soft.

    Los campos opcionales solo se incluyen si no son None, para no ensuciar el
    jsonb/columnas con valores vacíos.
    """
    if supa is None:
        return
    fila: dict = {
        "contrato_id": contrato_id,
        "etapa": etapa,
        "run_id": run_id or os.getenv("GITHUB_RUN_ID") or None,
        "detalle": _jsonable(detalle) if detalle else {},
    }
    if n_chunks_pdf is not None:
        fila["n_chunks_pdf"] = n_chunks_pdf
    if n_chunks_api is not None:
        fila["n_chunks_api"] = n_chunks_api
    if chars_tdr is not None:
        fila["chars_tdr"] = chars_tdr
    if tokens_est is not None:
        fila["tokens_est"] = tokens_est
    if costo_usd is not None:
        fila["costo_usd"] = round(float(costo_usd), 8)
    if tipo_extraccion is not None:
        fila["tipo_extraccion"] = tipo_extraccion
    if chunk_version is not None:
        fila["chunk_version"] = chunk_version
    try:
        supa.table("proceso_evento").insert(fila).execute()
    except Exception as e:
        print(f"  [warn] proceso_evento ({etapa} id={contrato_id}): {e}", flush=True)
