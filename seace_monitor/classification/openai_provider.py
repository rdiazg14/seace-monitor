"""Transporte OpenAI-compatible para clasificación (IA-005).

Espejo de ``gemini_provider.clasificar_lote_gemini`` sobre ``chat/completions``:
misma firma de composición, mismos hooks ``before_call``/``on_success`` (la
cuota C4 y los tokens viajan en el body normalizado a ``usageMetadata``) y la
misma política de reintentos acotados. ``response_format`` usa
``json_schema``; ``enable_thinking:false`` se fuerza a nivel raíz (hallazgo de
sandbox IA-001: Qwen cobra reasoning por defecto).
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from seace_monitor.ia.errores import ErrorProveedor
from seace_monitor.ia.openai import (
    mensaje_texto,
    post_openai,
    schema_openai,
    usage_metadata,
)


def clasificar_lote_openai(
    client: httpx.Client,
    lote: list[dict],
    *,
    system_prompt: str,
    schema: dict,
    armar_prompt: Callable[[list[dict]], str],
    api_key: str,
    url: str,
    parse_response: Callable[[str], list[dict]],
    modelo: str = "",
    params: dict | None = None,
    proveedor: str = "openai",
    before_call: Callable[[], None] | None = None,
    on_success: Callable[[dict], None] | None = None,
    quota_error_types: tuple[type[BaseException], ...] = (),
    backoff: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0, 32.0, 64.0),
    timeout: float = 120.0,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict]:
    """Solicita una clasificación estructurada vía API OpenAI-compatible.

    Los parámetros administrables (``params``) no pueden sustituir identidad,
    mensajes ni límites calculados por la aplicación (AUD-002).
    """
    payload = {
        **(params or {}),
        "model": modelo,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": armar_prompt(lote)},
        ],
        "temperature": 0,
        "max_tokens": 8192,
        "stream": False,
        "response_format": schema_openai(schema),
        "enable_thinking": False,
    }
    waits = (0.0, *backoff)
    last_error: Exception | None = None
    for attempt, wait in enumerate(waits):
        if wait:
            print(f"    [openai backoff {wait:.0f}s attempt={attempt}]", flush=True)
            sleep(wait)
        try:
            if before_call is not None:
                before_call()
            body = post_openai(
                client,
                url,
                api_key,
                payload,
                timeout=timeout,
                proveedor=proveedor,
                modelo=modelo or None,
            )
            if on_success is not None:
                on_success(usage_metadata(body))
            text = mensaje_texto(body, proveedor, modelo or None)
            return parse_response(text)
        except Exception as error:
            if quota_error_types and isinstance(error, quota_error_types):
                raise
            last_error = error
            if isinstance(error, ErrorProveedor):
                if error.kind == "rate_limit":
                    if error.retry_after:
                        extra = min(error.retry_after, 120.0)
                        print(f"    [429 Retry-After {extra:.0f}s]", flush=True)
                        sleep(extra)
                    continue
                if error.retriable:
                    print(f"    [retry {attempt}] {error}", flush=True)
                    continue
                raise
            print(f"    [retry {attempt}] {error}", flush=True)
    raise RuntimeError(f"clasificar_lote fallo: {last_error}")
