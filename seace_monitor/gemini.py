"""Helpers puros para hablar con Gemini (precios, parseo, fechas, vectores).

Centraliza funciones sin I/O que estaban duplicadas en los scripts del
pipeline. Los consumidores internos importan desde ``seace_monitor``; los
entrypoints raíz conservan solo las fachadas públicas necesarias para los CLI
y la compatibilidad externa.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

# Precios de referencia Gemini en USD por 1M de tokens.
FLASH_USD_IN_PER_M = 0.50   # gemini flash (OCR/clasificación): entrada
FLASH_USD_OUT_PER_M = 3.00  # gemini flash: salida
EMBED_USD_PER_M = 0.0375    # gemini-embedding-001: solo entrada (input-only)

_TZ_LIMA = timezone(timedelta(hours=-5))


def fecha_lima() -> str:
    """Fecha actual en la zona de Lima (UTC-5). Perú no tiene DST."""
    return datetime.now(_TZ_LIMA).date().isoformat()


def extract_gemini_text(body: dict) -> str:
    """Concatena el texto de salida de Gemini, saltando parts de razonamiento."""
    parts = (
        (body.get("candidates") or [{}])[0]
        .get("content", {})
        .get("parts") or []
    )
    textos = [p.get("text") or "" for p in parts if not p.get("thought")]
    return "".join(textos).strip()


def l2_normalize(vec: list[float]) -> list[float]:
    """Normaliza a norma L2. gemini-embedding-001 exige renormalizar si dim != 3072."""
    s = math.sqrt(sum(x * x for x in vec))
    if s <= 0:
        return vec
    return [x / s for x in vec]


def usd_flash(prompt: int, output: int) -> float:
    """Costo en USD de una llamada a gemini flash (entrada + salida)."""
    return (prompt / 1_000_000.0) * FLASH_USD_IN_PER_M + (
        output / 1_000_000.0
    ) * FLASH_USD_OUT_PER_M


def usd_embed(tokens: int) -> float:
    """Costo en USD de embed con gemini-embedding-001 (solo entrada)."""
    return tokens / 1_000_000.0 * EMBED_USD_PER_M
