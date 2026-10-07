"""Contingencia OCR por página (FIX-014).

El proveedor OCR principal es el que resuelve la config dinámica ``ocr``
(hoy Novita deepseek-ocr-2). Si agota sus reintentos internos — respuesta
vacía, timeouts o 5xx sostenidos — la página se reintenta una vez con
Gemini por ``GEMINI_API_KEY`` antes de marcarla fallida.

``CupoFlash`` nunca cae al respaldo: cuota/rate-limit agotada significa
"parar la corrida", no trasladar gasto silenciosamente a otro proveedor.
Cuando la cfg ya es Gemini el respaldo por env no aporta nada y tampoco
se reintenta.
"""

from __future__ import annotations

from seace_monitor.documents.pdf_extraction import limpiar_texto
from seace_monitor.ia.pipeline import fijar_ocr_activo, solicitar_ocr_cfg

from .gemini_provider import CupoFlash, solicitar_ocr_gemini


def ocr_con_contingencia(
    http,
    cfg,
    img_bytes: bytes,
    mime: str,
    *,
    gemini_key: str,
) -> str:
    """OCR por cfg; ante fallo agotado del proveedor, una pasada por Gemini."""
    try:
        return limpiar_texto(solicitar_ocr_cfg(http, cfg, img_bytes, mime))
    except CupoFlash:
        raise
    except Exception:
        if not gemini_key or getattr(cfg, "proveedor", None) == "gemini":
            raise
        # Telemetría: la página sale por el camino env (modelo/tarifa Gemini).
        fijar_ocr_activo(None)
        print(
            f"  [ocr] cfg {getattr(cfg, 'modelo', '?')} falló -> "
            "contingencia gemini env",
            flush=True,
        )
        return limpiar_texto(
            solicitar_ocr_gemini(http, img_bytes, mime, gemini_key))
