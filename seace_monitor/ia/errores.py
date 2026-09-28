"""Taxonomía de errores de proveedor IA — espejo de IA-003 (``src/ia/errores.ts``).

PLAN-001: distinguir autenticación (401/403), crédito agotado, rate limit,
timeout y 5xx. Nunca clasificar cualquier 429 como falta de saldo ni reintentar
sin límite. Los transportes de cada dominio traducen estos tipos a sus
excepciones históricas (``CupoFlash``, ``QuotaExceeded``, tope C4).
"""
from __future__ import annotations

import json

KINDS = (
    "credencial",    # 401/403 — la clave no sirve, no reintentar
    "cuota",         # saldo/cuota agotada (insufficient_quota) — no reintentar
    "rate_limit",    # 429 transitorio — reintento acotado
    "timeout",       # timeout del cliente/upstream — reintento acotado
    "server",        # 5xx del proveedor — reintento acotado
    "red",           # fallo de transporte sin respuesta — reintento acotado
    "invalid_json",  # 200 con cuerpo no parseable — no reintentar
    "vacio",         # respuesta vacía/sin contenido — reintento acotado
    "capacidad",     # el proveedor no soporta la capacidad pedida — no reintentar
    "http",          # otro status no clasificado — no reintentar
)

# Tipo de error OpenAI que representa saldo/cuota, no sobrecarga.
TIPOS_CUOTA = frozenset({"insufficient_quota", "billing_hard_limit_reached"})


class ErrorProveedor(Exception):
    """Error de proveedor con clasificación y sugerencia de reintento."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        proveedor: str,
        modelo: str | None = None,
        status: int | None = None,
        retriable: bool,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.proveedor = proveedor
        self.modelo = modelo
        self.status = status
        self.retriable = retriable
        self.retry_after = retry_after


def es_transitorio(error: BaseException) -> bool:
    """Timeout/red/5xx/rate-limit/vacío sí; el resto no."""
    if isinstance(error, ErrorProveedor):
        return error.retriable
    return False


def clasificar_http(
    status: int,
    cuerpo: str,
    proveedor: str,
    modelo: str | None = None,
    retry_after: float | None = None,
) -> ErrorProveedor:
    """Clasifica una respuesta HTTP de API OpenAI-compatible.

    ``cuerpo`` es el texto ya leído (acotado por el caller); ``retry_after``
    viene del header Retry-After cuando existe.
    """
    tipo: str | None = None
    try:
        parsed = json.loads(cuerpo)
        error = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(error, dict):
            tipos = (error.get("type"), error.get("code"))
            tipo = next((v for v in tipos if isinstance(v, str) and v in TIPOS_CUOTA), None)
    except (ValueError, AttributeError):
        pass  # cuerpo no-JSON: se conserva el texto crudo

    # La respuesta puede repetir claves, documentos o cabeceras. No publicarla.
    base = f"{proveedor} HTTP {status}{f' [{modelo}]' if modelo else ''}"

    def mk(kind: str, retriable: bool) -> ErrorProveedor:
        return ErrorProveedor(
            kind, base, proveedor=proveedor, modelo=modelo,
            status=status, retriable=retriable, retry_after=retry_after,
        )

    if status in (401, 403):
        return mk("credencial", False)
    if status == 402 or (tipo or "") in TIPOS_CUOTA:
        return mk("cuota", False)
    if status == 429:
        return mk("rate_limit", True)
    if status == 408 or status >= 500:
        return mk("server", True)
    return mk("http", False)


def error_timeout(proveedor: str, modelo: str | None = None) -> ErrorProveedor:
    """Timeout del cliente contra el proveedor."""
    return ErrorProveedor(
        "timeout",
        f"{proveedor} timeout{f' [{modelo}]' if modelo else ''}",
        proveedor=proveedor, modelo=modelo, retriable=True,
    )


def error_red(error: BaseException, proveedor: str, modelo: str | None = None) -> ErrorProveedor:
    """Fallo de transporte sin respuesta HTTP."""
    return ErrorProveedor(
        "red",
        f"{proveedor} red{f' [{modelo}]' if modelo else ''}",
        proveedor=proveedor, modelo=modelo, retriable=True,
    )
