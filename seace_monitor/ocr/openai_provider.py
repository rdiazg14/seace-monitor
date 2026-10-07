"""Transporte OpenAI-compatible para OCR de páginas e imágenes (IA-005).

La imagen viaja como ``image_url`` con data URI en ``chat/completions``
(contrato verificado con Novita en la puerta de IA-001). Escribe el mismo
canal de uso (``LAST_OCR_USAGE``/``OCR_USAGE_ACUM``) que el transporte Gemini,
así la cuota diaria, ``uso_ia`` y la persistencia quedan intactas.
429/saldo agotado se traduce a ``CupoFlash`` para que la corrida se pueda
reanudar, igual que el camino Gemini.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable

import httpx

from seace_monitor.ia.errores import ErrorProveedor
from seace_monitor.ia.openai import mensaje_texto, post_openai, usage_metadata

from .gemini_provider import (
    GEMINI_OCR_BACKOFF,
    LAST_OCR_USAGE,
    OCR_PROMPT,
    OCR_USAGE_ACUM,
    CupoFlash,
)

# FIX-014: los OCR especializados no procesan instrucciones largas — con el
# prompt conversacional genérico emiten EOS inmediato (choices[0].content="")
# o caen en bucles degenerados. Su contrato nativo es la instrucción corta
# del entrenamiento (Novita deepseek-ocr-2: "Free OCR."). La cfg puede fijar
# otro texto con params={"ocr_prompt": "..."} sin tocar código.
OCR_PROMPT_POR_PROVEEDOR = {"novita": "Free OCR."}


def _prompt_ocr(params: dict | None, proveedor: str) -> tuple[str, dict]:
    """Prompt efectivo y ``params`` sin la clave interna ``ocr_prompt``."""
    limpios = dict(params or {})
    prompt = (
        limpios.pop("ocr_prompt", None)
        or OCR_PROMPT_POR_PROVEEDOR.get(proveedor)
        or OCR_PROMPT
    )
    return prompt, limpios


def solicitar_ocr_openai(
    client: httpx.Client,
    image_bytes: bytes,
    mime: str,
    api_key: str,
    *,
    url: str,
    modelo: str = "",
    params: dict | None = None,
    proveedor: str = "openai",
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = 120.0,
) -> str:
    """Envía una imagen a un chat/completions con visión y devuelve el texto."""
    encoded = base64.b64encode(image_bytes).decode("ascii")
    prompt, params_limpios = _prompt_ocr(params, proveedor)
    payload = {
        **params_limpios,
        "model": modelo,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}"},
                },
            ],
        }],
        "temperature": 0.1,
        "max_tokens": 4096,
        "stream": False,
        "enable_thinking": False,
    }
    last_error: Exception | None = None
    for wait in GEMINI_OCR_BACKOFF:
        if wait:
            sleep(wait)
        try:
            body = post_openai(
                client,
                url,
                api_key,
                payload,
                timeout=timeout,
                proveedor=proveedor,
                modelo=modelo or None,
            )
            usage = usage_metadata(body)["usageMetadata"]
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
            return mensaje_texto(body, proveedor, modelo or None)
        except CupoFlash:
            raise
        except ErrorProveedor as error:
            if error.kind in ("rate_limit", "cuota"):
                raise CupoFlash(
                    str(error)[:200],
                    motivo="429" if error.kind == "rate_limit" else "cuota",
                ) from error
            if not error.retriable:
                raise
            last_error = error
        except Exception:
            raise ErrorProveedor("invalid_json", "Respuesta OCR inválida",
                proveedor=proveedor, modelo=modelo, retriable=False) from None
    raise RuntimeError(f"OCR OpenAI fallo: {last_error}")
