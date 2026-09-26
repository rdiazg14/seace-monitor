#!/usr/bin/env python3
"""Compatibilidad: la persistencia vive en ``seace_monitor.classification``."""

from seace_monitor.classification.repository import (
    anunciar_backend_capa3,
    conectar_pg,
    diff_clasificacion_contratos,
    diff_ids_supa,
    escribir_gemini,
    escribir_keyword,
    map_confianza,
    upsert_gemini,
    upsert_gemini_supa,
    upsert_keyword,
    upsert_keyword_supa,
)

__all__ = [
    "anunciar_backend_capa3",
    "conectar_pg",
    "diff_clasificacion_contratos",
    "diff_ids_supa",
    "escribir_gemini",
    "escribir_keyword",
    "map_confianza",
    "upsert_gemini",
    "upsert_gemini_supa",
    "upsert_keyword",
    "upsert_keyword_supa",
]
