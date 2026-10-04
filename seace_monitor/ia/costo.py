"""Costo estimado de llamadas IA del pipeline — FIX-008.

Misma regla que ``seace-ai-proxy/src/telemetry/costo.ts``: ``ia_modelos.precio``
validado, tramo elegido por los tokens de entrada reales de la llamada
(``hasta_input_tokens`` inclusivo; sobre el último tramo se aplica el último y
se marca). Sin precio válido no se inventa un valor: el costo queda ``None``.
"""
from __future__ import annotations

import math
from typing import Any


def _valido(v: Any) -> bool:
    return (
        isinstance(v, (int, float)) and not isinstance(v, bool)
        and math.isfinite(v) and v >= 0
    )


def parse_precio(raw: Any) -> dict | None:
    """Normaliza ``ia_modelos.precio``; ``None`` si falta, no es USD o es inválido."""
    if not isinstance(raw, dict) or not raw:
        return None
    if raw.get("moneda") is not None and str(raw["moneda"]).upper() != "USD":
        return None
    out = raw.get("out", 0)
    if not _valido(raw.get("in")) or not _valido(out):
        return None
    tramos: list[dict] = []
    tiers = raw.get("tiers")
    if tiers is not None:
        if not isinstance(tiers, list):
            return None
        previo = 0
        for t in tiers:
            t = t if isinstance(t, dict) else {}
            hasta = t.get("hasta_input_tokens")
            t_out = t.get("out", 0)
            if (not isinstance(hasta, int) or isinstance(hasta, bool) or hasta <= previo
                    or not _valido(t.get("in")) or not _valido(t_out)):
                return None
            tramos.append({"hasta": hasta, "in": float(t["in"]), "out": float(t_out)})
            previo = hasta
    precio = {"in": float(raw["in"]), "out": float(out), "tramos": tramos}
    if isinstance(raw.get("fecha"), str):
        precio["fecha"] = raw["fecha"]
    return precio


def tarifa(precio: dict, prompt_tokens: int) -> tuple[float, float, int | None, bool]:
    """(usd_in, usd_out, tramo, excede) aplicable a ``prompt_tokens``."""
    tramos = precio.get("tramos") or []
    if not tramos:
        return precio["in"], precio["out"], None, False
    for i, t in enumerate(tramos):
        if prompt_tokens <= t["hasta"]:
            return t["in"], t["out"], i, False
    ultimo = len(tramos) - 1
    return tramos[ultimo]["in"], tramos[ultimo]["out"], ultimo, True


def estimar(
    precio: dict | None,
    prompt: int,
    salida: int,
    *,
    llamadas: int = 1,
    fuente: str = "config",
) -> tuple[float | None, dict]:
    """Costo USD y trazabilidad para ``uso_ia.detalle``.

    ``llamadas`` > 1 indica tokens acumulados de varias llamadas: el tramo se
    elige con el promedio de entrada por llamada. ``fuente`` distingue la
    configuración dinámica de la tabla del camino por env.
    """
    if precio is None:
        return None, {"precio_fuente": "desconocido", "costo_estimado": True}
    por_llamada = math.ceil(prompt / max(llamadas, 1))
    usd_in, usd_out, tramo, excede = tarifa(precio, por_llamada)
    detalle: dict = {"precio_fuente": fuente, "costo_estimado": True}
    if tramo is not None:
        detalle["precio_tramo"] = tramo
    if excede:
        detalle["precio_excede_tramos"] = True
    if precio.get("fecha"):
        detalle["precio_fecha"] = precio["fecha"]
    return prompt / 1_000_000.0 * usd_in + salida / 1_000_000.0 * usd_out, detalle
