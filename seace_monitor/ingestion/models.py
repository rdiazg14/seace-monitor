"""Validación y saneamiento de registros recibidos desde SEACE."""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator


class RegistroSeace(BaseModel):
    """Campos requeridos por la ingesta; SEACE puede enviar campos adicionales."""

    model_config = ConfigDict(extra="allow")

    idContrato: int
    nroContratacion: int | str | None = None
    desContratacion: str | None = None
    nomObjetoContrato: str | None = None
    desObjetoContrato: str | None = None
    nomEntidad: str | None = None
    nomEstadoContrato: str | None = None
    fecPublica: str | None = None
    fecIniCotizacion: str | None = None
    fecFinCotizacion: str | None = None
    idTipoCotizacion: int | str | None = None
    cotizar: bool | None = None

    @field_validator("idContrato")
    @classmethod
    def id_positivo(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("idContrato debe ser > 0")
        return value

    @field_validator(
        "desContratacion", "nomObjetoContrato", "desObjetoContrato",
        "nomEntidad", "nomEstadoContrato", "fecPublica",
        "fecIniCotizacion", "fecFinCotizacion", mode="before",
    )
    @classmethod
    def vacio_a_none(cls, value):
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            raise ValueError("se esperaba texto, llegó estructura")
        cleaned = str(value).strip()
        return cleaned or None


def id_contrato_de(payload: dict) -> int | None:
    try:
        value = int(payload.get("idContrato"))
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


_KEYS_SESION = {
    "cookie", "cookies", "authorization", "token", "access_token",
    "refresh_token", "set-cookie", "headers", "header", "csrf",
    "x-csrf-token", "api_key", "apikey", "session", "playwright",
    "request", "response",
}


def payload_solo_datos(registro: dict) -> dict:
    """Copia datos de negocio y descarta contexto HTTP o de sesión."""
    output: dict = {}
    for key, value in registro.items():
        normalized = str(key).lower()
        if normalized in _KEYS_SESION or "cookie" in normalized or "token" in normalized:
            continue
        if normalized.startswith("authorization") or normalized.startswith("x-"):
            continue
        output[key] = value
    return output


def filtrar_validos(
    raw: list[dict],
    on_reject: Callable[[dict, str], None],
) -> tuple[list[dict], int]:
    """Valida el lote y entrega cada rechazo al repositorio del caller."""
    accepted: list[dict] = []
    rejected = 0
    for row in raw:
        if not isinstance(row, dict):
            rejected += 1
            on_reject({"_raw": row}, "registro no es un objeto JSON")
            continue
        try:
            RegistroSeace.model_validate(row)
            accepted.append(row)
        except ValidationError as error:
            rejected += 1
            on_reject(row, str(error))
    return accepted, rejected
