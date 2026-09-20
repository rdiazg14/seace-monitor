from __future__ import annotations

from datetime import datetime

from seace_monitor.seace_api import (
    API_BUSCADOR,
    API_DETALLE,
    SPA_BASE,
    SPA_URL,
    parsear_fecha,
)


def test_constantes_endpoints():
    assert SPA_BASE == "https://prod6.seace.gob.pe"
    assert SPA_URL.endswith("/buscador-publico/contrataciones")
    assert API_BUSCADOR.endswith("/contrataciones/buscador")
    assert API_DETALLE.endswith("/contrataciones/listar-completo")
    assert API_BUSCADOR.startswith(SPA_BASE)
    assert API_DETALLE.startswith(SPA_BASE)


def test_parsear_fecha_vacia():
    assert parsear_fecha(None) is None
    assert parsear_fecha("") is None
    assert parsear_fecha("   ") is None


def test_parsear_fecha_valida():
    out = parsear_fecha("15/08/2026 10:30:00")
    assert out == "2026-08-15T10:30:00-05:00"


def test_parsear_fecha_malformada():
    assert parsear_fecha("no-es-fecha") is None
    assert parsear_fecha("2026-08-15") is None


def test_parsear_fecha_corrupta_futuro():
    # Año fuera de rango (corrupción legacy) → None.
    assert parsear_fecha("15/08/4202 10:30:00") is None
    assert parsear_fecha("15/08/2052 00:00:00") is None


def test_parsear_fecha_corrupta_pasado():
    assert parsear_fecha("15/08/1900 00:00:00") is None


def test_parsear_fecha_ignora_espacios():
    out = parsear_fecha("  15/08/2026 10:30:00  ")
    assert out == "2026-08-15T10:30:00-05:00"
