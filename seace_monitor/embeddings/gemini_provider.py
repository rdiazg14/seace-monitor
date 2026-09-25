"""Transporte HTTP de Gemini para embeddings.

No lee variables de entorno ni crea clientes. El caller conserva la custodia
de credenciales, la orquestación de lotes y la persistencia de resultados.
"""
from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from seace_monitor.embeddings.preparation import EMBED_STATS
from seace_monitor.gemini import l2_normalize


GEMINI_EMBED_MODEL = "gemini-embedding-001"
GEMINI_DIM = 1536
GEMINI_EMBED_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_EMBED_MODEL}:batchEmbedContents"
)
GEMINI_AUTH_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_EMBED_MODEL}:embedContent"
)
GEMINI_BACKOFF = (2.0, 4.0, 8.0, 16.0, 32.0, 64.0)


class QuotaExceeded(RuntimeError):
    """Gemini respondió 429 y el caller pidió detenerse inmediatamente."""


def solicitar_embeddings_gemini(
    client: httpx.Client,
    texts: list[str],
    api_key: str,
    fail_fast: bool = False,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> list[list[float]]:
    """Solicita y normaliza un lote, conservando la política de reintentos."""
    payload = {
        "requests": [
            {
                "model": f"models/{GEMINI_EMBED_MODEL}",
                "content": {"parts": [{"text": text}]},
                "taskType": "RETRIEVAL_DOCUMENT",
                "outputDimensionality": GEMINI_DIM,
            }
            for text in texts
        ]
    }
    waits = [0.0] if fail_fast else [0.0] + list(GEMINI_BACKOFF)
    last_error: Exception | None = None
    for attempt, wait in enumerate(waits):
        if wait:
            print(f"    [gemini backoff {wait:.0f}s attempt={attempt}]", flush=True)
            sleep(wait)
        try:
            response = client.post(
                GEMINI_EMBED_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key,
                },
                json=payload,
                timeout=120.0,
            )
            if response.status_code == 429:
                message = f"429 {response.text[:200]}"
                if fail_fast:
                    raise QuotaExceeded(message)
                last_error = RuntimeError(message)
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        sleep(min(float(retry_after), 120.0))
                    except ValueError:
                        pass
                continue
            response.raise_for_status()
            body = response.json()
            raw = body.get("embeddings")
            if not isinstance(raw, list) or len(raw) != len(texts):
                raise RuntimeError(
                    f"gemini respuesta inesperada keys={list(body)[:8]} "
                    f"n={len(raw) if isinstance(raw, list) else None}"
                )
            EMBED_STATS["requests"] += 1
            EMBED_STATS["texts"] += len(texts)
            EMBED_STATS["chars"] += sum(len(text) for text in texts)
            usage = body.get("usageMetadata") or {}
            tokens = usage.get("totalTokenCount") or usage.get("promptTokenCount") or 0
            try:
                EMBED_STATS["tokens_api"] += int(tokens or 0)
            except (TypeError, ValueError):
                pass
            if EMBED_STATS["requests"] == 1:
                print(f"  gemini keys={list(body)[:12]} usage={usage or '—'}", flush=True)
            out: list[list[float]] = []
            for item in raw:
                values = item.get("values") if isinstance(item, dict) else None
                if not isinstance(values, list) or not values:
                    raise RuntimeError("gemini embedding vacío")
                if len(values) > GEMINI_DIM:
                    values = values[:GEMINI_DIM]
                if len(values) != GEMINI_DIM:
                    raise RuntimeError(f"dimensión {len(values)} != {GEMINI_DIM}")
                out.append(l2_normalize([float(value) for value in values]))
            return out
        except QuotaExceeded:
            raise
        except httpx.HTTPStatusError as error:
            last_error = error
            code = error.response.status_code if error.response is not None else 0
            if fail_fast and code == 429:
                raise QuotaExceeded(str(error)) from error
            if error.response is not None and code in (429, 500, 503):
                if fail_fast:
                    raise
                print(f"    [retry {attempt}] HTTP {code}", flush=True)
                continue
            raise
        except Exception as error:
            last_error = error
            print(f"    [retry {attempt}] {error}", flush=True)
            if fail_fast:
                raise
    raise RuntimeError(f"embed_lote_gemini falló: {last_error}")


def consultar_auth_gemini(client, api_key: str) -> int:
    """Ejecuta el ping de embeddings y devuelve únicamente su código HTTP."""
    response = client.post(
        GEMINI_AUTH_URL,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        json={
            "model": f"models/{GEMINI_EMBED_MODEL}",
            "content": {"parts": [{"text": "ok"}]},
            "taskType": "RETRIEVAL_DOCUMENT",
            "outputDimensionality": GEMINI_DIM,
        },
        timeout=30.0,
    )
    return response.status_code
