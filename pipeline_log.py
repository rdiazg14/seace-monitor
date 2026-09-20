"""Shim de retrocompatibilidad.

El módulo canónico ahora es `seace_monitor.logging`. Este archivo se conserva
para no romper a los scripts que aún importan `pipeline_log` (p. ej.
descargar_requerimiento.py) y a referencias externas.
"""
from __future__ import annotations

from seace_monitor.logging import (  # noqa: F401
    PASO_CAPAS,
    PASO_CHUNKING,
    PASO_CONTENEDORES,
    PASO_EMBEDDING,
    PASO_INGESTA,
    PASO_OCR,
    PASO_PDF,
    registrar_evento,
    registrar_run,
)
