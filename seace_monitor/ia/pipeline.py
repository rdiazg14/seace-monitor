"""Composición de consumidores IA con la configuración dinámica ia_*.

Cada entrypoint resuelve su endpoint ('clasificar', 'ocr', 'embeddings')
mediante el Resolver compartido (cache TTL 60s, fail-soft, invalidación con
`invalidate`). Si la config no es utilizable —inexistente, inactiva, modelo o
proveedor inactivo, clave ilegible— se devuelve None/{} y el llamador sigue su
camino histórico por variables de entorno.
"""
from __future__ import annotations

from functools import partial
from typing import Any

from seace_monitor.gemini import (
    FLASH_USD_IN_PER_M,
    FLASH_USD_OUT_PER_M,
)
from seace_monitor.ia.openai import url_openai
from seace_monitor.ia.resolver import (
    ConfigEmbeddingInsegura,
    ModeloCfg,
    clave_efectiva,
    resolver_compartido,
)

DIMENSIONES_EMBEDDING_V2 = 1536  # espacio activo gemini-emb001-1536 (DATOS)


def cfg_resuelta(endpoint: str) -> ModeloCfg | None:
    """Costura única para resolver un endpoint dinámico (fail-soft)."""
    return resolver_compartido().resolver(endpoint)


def clave_config(cfg: ModeloCfg | None) -> str:
    return clave_efectiva(cfg) if cfg is not None else ""


def cfg_con_credencial(
    endpoint: str,
    tipos: tuple[str, ...] = ("openai", "gemini"),
    resolver=None,
) -> ModeloCfg | None:
    """Endpoint activo con clave utilizable, o None (→ camino por env).

    ``resolver`` permite al entrypoint inyectar su costura de resolución;
    por defecto usa el resolver compartido del proceso.
    """
    resolver = resolver or cfg_resuelta
    cfg = resolver(endpoint)
    if cfg is None or cfg.tipo_api not in tipos:
        return None
    if not clave_config(cfg):
        print(
            f"  [aviso] ia_endpoints.{endpoint} sin clave utilizable "
            f"({cfg.proveedor}/{cfg.modelo}); camino por env",
            flush=True,
        )
        return None
    return cfg


def embeddings_configurados(resolver=None) -> bool:
    """Predicado de habilitación: nunca aborta la corrida documental.

    Si el corpus no se puede verificar, la corrida sigue (texto/OCR/chunks) y
    el rechunk de cada contrato se detiene con ``ConfigEmbeddingInsegura``.
    """
    try:
        return bool(extras_embeddings((resolver or cfg_resuelta)("embeddings")))
    except ConfigEmbeddingInsegura:
        return False


def fijar_ocr_activo(cfg: ModeloCfg | None) -> None:
    """Publica modelo/tarifa del proveedor OCR para cuota y uso_ia."""
    from seace_monitor.ocr.gemini_provider import GEMINI_FLASH, OCR_ACTIVO

    if cfg is None:
        OCR_ACTIVO.update(modelo=GEMINI_FLASH, usd_in=FLASH_USD_IN_PER_M,
                          usd_out=FLASH_USD_OUT_PER_M, version_config=0)
        return
    precio = cfg.precio or {}
    OCR_ACTIVO.update({
        "modelo": cfg.modelo or GEMINI_FLASH,
        "usd_in": float(precio.get("in", FLASH_USD_IN_PER_M)),
        "usd_out": float(precio.get("out", FLASH_USD_OUT_PER_M)),
        "version_config": cfg.version_config,
    })


def url_gemini_generate(cfg: ModeloCfg) -> str:
    return (
        f"{(cfg.base_url or '').rstrip('/')}/models/"
        f"{cfg.modelo}:generateContent"
    )


def solicitar_ocr_cfg(
    client,
    cfg: ModeloCfg,
    img_bytes: bytes,
    mime: str,
) -> str:
    """OCR vía config dinámica (openai/gemini). El camino por env no pasa aquí."""
    from seace_monitor.ocr.gemini_provider import solicitar_ocr_gemini
    from seace_monitor.ocr.openai_provider import solicitar_ocr_openai

    fijar_ocr_activo(cfg)
    key = clave_config(cfg)
    if cfg.tipo_api == "openai":
        return solicitar_ocr_openai(
            client,
            img_bytes,
            mime,
            key,
            url=url_openai(cfg.base_url or "", "chat/completions"),
            modelo=cfg.modelo,
            params=cfg.params,
            proveedor=cfg.proveedor,
            timeout=cfg.timeout_ms / 1000.0,
        )
    return solicitar_ocr_gemini(
        client, img_bytes, mime, key, url=url_gemini_generate(cfg),
        timeout=cfg.timeout_ms / 1000.0,
    )


def extras_embeddings(cfg: ModeloCfg | None) -> dict[str, Any]:
    """Kwargs para run_gemini/rechunk según la config dinámica 'embeddings'."""
    if cfg is None or cfg.tipo_api not in ("openai", "gemini"):
        return {}
    key = clave_config(cfg)
    if not key:
        return {}
    dim = cfg.dimensiones or DIMENSIONES_EMBEDDING_V2
    if dim != DIMENSIONES_EMBEDDING_V2:
        # embedding_v2/espacio gemini-emb001-1536: abortar antes de mezclar.
        raise SystemExit(
            f"ERROR: ia_endpoints.embeddings configura {dim} dimensiones "
            f"({cfg.modelo}); el corpus activo es {DIMENSIONES_EMBEDDING_V2}. "
            "La migración de espacio vectorial corresponde a IA-007."
        )
    extras: dict[str, Any] = {
        "api_key": key,
        "modelo": cfg.modelo,
        "version_config": cfg.version_config,
    }
    def verificar_config() -> None:
        actual = cfg_resuelta("embeddings")
        campos = ("version_config", "proveedor", "modelo", "dimensiones", "espacio_vectorial")
        if actual is None or any(getattr(actual, c) != getattr(cfg, c) for c in campos):
            raise ConfigEmbeddingInsegura("Configuración embeddings cambió durante el lote; detener y reanudar")
    extras["verificar_config"] = verificar_config
    precio_in = (cfg.precio or {}).get("in")
    if precio_in is not None:
        extras["precio_in"] = float(precio_in)
    if cfg.tipo_api == "openai":
        from seace_monitor.embeddings.openai_provider import (
            solicitar_embeddings_openai,
        )

        extras["solicitar"] = partial(
            solicitar_embeddings_openai,
            url=url_openai(cfg.base_url or "", "embeddings"),
            modelo=cfg.modelo,
            dimensiones=dim,
            batch_max=int((cfg.capacidades or {}).get("batch_max") or 10),
            params=cfg.params,
            proveedor=cfg.proveedor,
            timeout=cfg.timeout_ms / 1000.0,
        )
    else:
        from seace_monitor.embeddings.gemini_provider import (
            solicitar_embeddings_gemini,
        )

        extras["solicitar"] = partial(
            solicitar_embeddings_gemini,
            url=(
                f"{(cfg.base_url or '').rstrip('/')}/models/"
                f"{cfg.modelo}:batchEmbedContents"
            ),
            modelo=cfg.modelo,
            dim=dim,
            timeout=cfg.timeout_ms / 1000.0,
        )
    return extras
