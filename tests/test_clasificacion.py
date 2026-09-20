"""Tests de seace_monitor.clasificacion (reglas determinísticas, sin BD/red)."""
from __future__ import annotations

from seace_monitor.clasificacion import (
    _contiene,
    _norm,
    clasificar_categoria_it,
    clasificar_relevancia_ia,
)


def test_norm_minusculas_y_sin_tildes():
    assert _norm("ÁÉÍÓÚ ñ Ñ") == "aeiou n n"
    assert _norm("") == ""


def test_categoria_primera_coincidencia_gana():
    r = {"desObjetoContrato": "servicio de firma digital y machine learning"}
    assert clasificar_categoria_it(r) == "Firma digital"


def test_categoria_sin_match_es_none():
    r = {"desObjetoContrato": "servicio de limpieza de oficinas"}
    assert clasificar_categoria_it(r) is None


def test_categoria_hardware():
    r = {"desObjetoContrato": "adquisicion de laptops y computadoras"}
    assert clasificar_categoria_it(r) == "Hardware"


def test_relevancia_alta():
    assert clasificar_relevancia_ia({"desObjetoContrato": "integracion con gemini"}) == "ALTA"


def test_relevancia_media_dos_genericos():
    r = {"desObjetoContrato": "inteligencia artificial y chatbot"}
    assert clasificar_relevancia_ia(r) == "MEDIA"


def test_relevancia_baja_un_generico():
    r = {"desObjetoContrato": "solucion con inteligencia artificial"}
    assert clasificar_relevancia_ia(r) == "BAJA"


def test_relevancia_none():
    assert clasificar_relevancia_ia({"desObjetoContrato": "servicio de limpieza"}) is None


def test_contiene_limite_palabra_ia():
    # "ia" exige límite de palabra: no matchea dentro de "inteligencia".
    assert _contiene(" inteligencia ", "ia") is False
    assert _contiene(" plataforma ia generativa ", "ia") is True
