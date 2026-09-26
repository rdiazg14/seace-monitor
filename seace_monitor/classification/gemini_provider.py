"""Transporte Gemini para clasificación, sin configuración ni cuota global."""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from seace_monitor.gemini import extract_gemini_text


def clasificar_lote_gemini(
    client: httpx.Client,
    lote: list[dict],
    *,
    system_prompt: str,
    schema: dict,
    armar_prompt: Callable[[list[dict]], str],
    api_key: str,
    url: str,
    parse_response: Callable[[str], list[dict]],
    before_call: Callable[[], None] | None = None,
    on_success: Callable[[dict], None] | None = None,
    quota_error_types: tuple[type[BaseException], ...] = (),
    backoff: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0, 32.0, 64.0),
    timeout: float = 120.0,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict]:
    """Solicita una clasificación estructurada y diferencia cuota del transporte."""
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": armar_prompt(lote)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "thinkingConfig": {"thinkingLevel": "LOW"},
            "temperature": 0,
            "maxOutputTokens": 8192,
        },
    }
    waits = (0.0, *backoff)
    last_error: Exception | None = None
    for attempt, wait in enumerate(waits):
        if wait:
            print(f"    [gemini backoff {wait:.0f}s attempt={attempt}]", flush=True)
            sleep(wait)
        try:
            if before_call is not None:
                before_call()
            response = client.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key,
                },
                json=payload,
                timeout=timeout,
            )
            if response.status_code == 429:
                last_error = RuntimeError(f"429 {response.text[:200]}")
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        extra_wait = min(float(retry_after), 120.0)
                        print(f"    [429 Retry-After {extra_wait:.0f}s]", flush=True)
                        sleep(extra_wait)
                    except ValueError:
                        pass
                continue
            response.raise_for_status()
            body = response.json()
            if on_success is not None:
                on_success(body)
            text = extract_gemini_text(body)
            if not text:
                raise RuntimeError("gemini vacio")
            return parse_response(text)
        except Exception as error:
            if quota_error_types and isinstance(error, quota_error_types):
                raise
            last_error = error
            if isinstance(error, httpx.HTTPStatusError):
                code = error.response.status_code if error.response is not None else 0
                if code in (429, 500, 503):
                    print(f"    [retry {attempt}] HTTP {code}", flush=True)
                    continue
                raise
            print(f"    [retry {attempt}] {error}", flush=True)
    raise RuntimeError(f"clasificar_lote fallo: {last_error}")
