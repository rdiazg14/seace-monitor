"""Transporte OpenAI-compatible para embeddings (IA-005).

Espejo de ``gemini_provider.solicitar_embeddings_gemini``: lotes acotados por
``batch_max`` (text-embedding-v4: ≤10), dimensión explícita y salida
L2-normalizada con validación exacta — la misma invariante que exige el
corpus ``buscar_tdr_v2``. ``EMBED_STATS`` es el canal de uso compartido.
429 se traduce a ``QuotaExceeded`` en ``fail_fast``; saldo agotado
(``insufficient_quota``) lo hace siempre, porque reintentar no recupera saldo.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from seace_monitor.embeddings.preparation import EMBED_STATS
from seace_monitor.gemini import l2_normalize
from seace_monitor.ia.errores import ErrorProveedor
from seace_monitor.ia.openai import post_openai

from .gemini_provider import GEMINI_BACKOFF, QuotaExceeded


def solicitar_embeddings_openai(
    client: httpx.Client,
    texts: list[str],
    api_key: str,
    fail_fast: bool = False,
    *,
    url: str,
    modelo: str,
    dimensiones: int,
    batch_max: int = 10,
    params: dict | None = None,
    proveedor: str = "openai",
    backoff: tuple[float, ...] = GEMINI_BACKOFF,
    timeout: float = 120.0,
    sleep: Callable[[float], None] = time.sleep,
) -> list[list[float]]:
    """Solicita embeddings por lotes; devuelve vectores normalizados."""
    out: list[list[float]] = []
    for i in range(0, len(texts), max(1, batch_max)):
        out.extend(
            _lote_openai(
                client,
                texts[i:i + batch_max],
                api_key,
                fail_fast,
                url=url,
                modelo=modelo,
                dimensiones=dimensiones,
                params=params,
                proveedor=proveedor,
                backoff=backoff,
                timeout=timeout,
                sleep=sleep,
            )
        )
    return out


def _lote_openai(
    client: httpx.Client,
    texts: list[str],
    api_key: str,
    fail_fast: bool,
    *,
    url: str,
    modelo: str,
    dimensiones: int,
    params: dict | None,
    proveedor: str,
    backoff: tuple[float, ...],
    timeout: float,
    sleep: Callable[[float], None],
) -> list[list[float]]:
    payload = {
        **(params or {}),
        "model": modelo,
        "input": [t[:8000] for t in texts],
        "dimensions": dimensiones,
    }
    waits = [0.0] if fail_fast else [0.0] + list(backoff)
    last_error: Exception | None = None
    for attempt, wait in enumerate(waits):
        if wait:
            print(f"    [openai backoff {wait:.0f}s attempt={attempt}]", flush=True)
            sleep(wait)
        try:
            body = post_openai(
                client,
                url,
                api_key,
                payload,
                timeout=timeout,
                proveedor=proveedor,
                modelo=modelo,
            )
            raw = body.get("data")
            if not isinstance(raw, list) or len(raw) != len(texts):
                raise ErrorProveedor(
                    "vacio",
                    f"{proveedor} embeddings: "
                    f"{len(raw) if isinstance(raw, list) else None}/{len(texts)}",
                    proveedor=proveedor, modelo=modelo, retriable=True,
                )
            ordenados = sorted(
                raw, key=lambda item: (item or {}).get("index") or 0
            )
            vectores: list[list[float]] = []
            for item in ordenados:
                values = (item or {}).get("embedding")
                if not isinstance(values, list) or len(values) != dimensiones:
                    raise ErrorProveedor(
                        "invalid_json",
                        f"{proveedor} embed dim "
                        f"{len(values) if isinstance(values, list) else None}"
                        f" != {dimensiones}",
                        proveedor=proveedor, modelo=modelo, retriable=False,
                    )
                vectores.append(l2_normalize([float(v) for v in values]))
            EMBED_STATS["requests"] += 1
            EMBED_STATS["texts"] += len(texts)
            EMBED_STATS["chars"] += sum(len(t) for t in texts)
            usage = body.get("usage") or {}
            tokens = usage.get("total_tokens") or usage.get("prompt_tokens") or 0
            try:
                EMBED_STATS["tokens_api"] += int(tokens or 0)
            except (TypeError, ValueError):
                pass
            return vectores
        except QuotaExceeded:
            raise
        except ErrorProveedor as error:
            last_error = error
            if error.kind == "cuota":
                # Sin saldo no hay reintento útil; equivale al 429 fail-fast.
                raise QuotaExceeded(str(error)) from error
            if error.kind == "rate_limit":
                if fail_fast:
                    raise QuotaExceeded(str(error)) from error
                if error.retry_after:
                    try:
                        sleep(min(float(error.retry_after), 120.0))
                    except (TypeError, ValueError):
                        pass
                continue
            if not error.retriable:
                raise
            print(f"    [retry {attempt}] {error}", flush=True)
        except Exception as error:
            last_error = error
            print(f"    [retry {attempt}] {error}", flush=True)
            if fail_fast:
                raise
    raise RuntimeError(f"embed_lote_openai falló: {last_error}")
