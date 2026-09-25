"""Transporte HTTP de Gemini para OCR de páginas e imágenes.

No lee variables de entorno ni aplica límites operativos. El caller conserva
la custodia de la clave, el control RPM, los cupos y la persistencia.
"""
from __future__ import annotations

import base64
import time
from collections.abc import Callable

GEMINI_FLASH = "gemini-3.7-flash"
GEMINI_OCR_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_FLASH}:generateContent"
)
GEMINI_OCR_BACKOFF = (0.0, 2.0, 8.0, 20.0)
OCR_PROMPT = (
    "Extrae TODO el texto visible de esta pagina de un "
    "requerimiento/TDR de contratacion publica peruana (SEACE). "
    "Responde solo el texto, en espanol, sin preambulo ni markdown."
)

LAST_OCR_USAGE: dict = {}
OCR_USAGE_ACUM: dict = {"prompt": 0, "candidates": 0, "total": 0, "llamadas": 0}


class CupoFlash(Exception):
    """Tope operativo o respuesta 429 que permite reanudar el OCR después."""

    def __init__(self, message: str, motivo: str = "cupo"):
        super().__init__(message)
        self.motivo = motivo


def solicitar_ocr_gemini(
    client,
    image_bytes: bytes,
    mime: str,
    api_key: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Envía una imagen y devuelve el texto visible informado por Gemini."""
    encoded = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": OCR_PROMPT},
                {"inlineData": {"mimeType": mime, "data": encoded}},
            ],
        }],
        "generationConfig": {
            "thinkingConfig": {"thinkingLevel": "LOW"},
            "maxOutputTokens": 4096,
            "temperature": 0.1,
        },
    }
    last_error: Exception | None = None
    for wait in GEMINI_OCR_BACKOFF:
        if wait:
            sleep(wait)
        try:
            response = client.post(
                GEMINI_OCR_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key,
                },
                json=payload,
                timeout=120.0,
            )
            if response.status_code == 429:
                raise CupoFlash(f"429 OCR: {response.text[:160]}", motivo="429")
            response.raise_for_status()
            body = response.json()
            usage = body.get("usageMetadata") or {}
            prompt_tokens = int(usage.get("promptTokenCount") or 0)
            candidate_tokens = int(usage.get("candidatesTokenCount") or 0)
            total_tokens = int(usage.get("totalTokenCount") or 0)
            LAST_OCR_USAGE.clear()
            LAST_OCR_USAGE.update({
                "prompt": prompt_tokens,
                "candidates": candidate_tokens,
                "total": total_tokens,
            })
            OCR_USAGE_ACUM["prompt"] += prompt_tokens
            OCR_USAGE_ACUM["candidates"] += candidate_tokens
            OCR_USAGE_ACUM["total"] += total_tokens
            OCR_USAGE_ACUM["llamadas"] += 1
            parts = (
                (body.get("candidates") or [{}])[0]
                .get("content", {})
                .get("parts") or []
            )
            texts = [
                part.get("text") or ""
                for part in parts
                if not part.get("thought")
            ]
            return "\n".join(texts)
        except CupoFlash:
            raise
        except Exception as error:
            last_error = error
    raise RuntimeError(f"OCR Gemini fallo: {last_error}")
