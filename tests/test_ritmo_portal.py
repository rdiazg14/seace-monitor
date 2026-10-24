"""Pausa mínima entre peticiones de detalle y archivos al portal.

El 23/10/2026 el portal rechazó por unos minutos la dirección que pidió unas
13 descargas seguidas sin pausa; con una pausa de 20 s entre procesos aceptó
42 descargas seguidas sin rechazos.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from seace_monitor import seace_api
from seace_monitor.documents.seace_files import SeaceHttp
from seace_monitor.seace_api import PAUSA_SEGUNDOS_DEFECTO, Ritmo, pausa_configurada

ROOT = Path(__file__).resolve().parent.parent


class _Reloj:
    def __init__(self) -> None:
        self.t = 100.0
        self.esperas: list[float] = []

    def __call__(self) -> float:
        return self.t

    def dormir(self, segundos: float) -> None:
        self.esperas.append(round(segundos, 3))
        self.t += segundos


def test_la_primera_peticion_no_espera() -> None:
    reloj = _Reloj()
    Ritmo(10, reloj=reloj, dormir=reloj.dormir).esperar()
    assert reloj.esperas == []


def test_espera_solo_lo_que_falta_del_intervalo() -> None:
    reloj = _Reloj()
    ritmo = Ritmo(10, reloj=reloj, dormir=reloj.dormir)
    ritmo.esperar()
    reloj.t += 3  # la petición anterior tardó 3 s
    ritmo.esperar()
    reloj.t += 12  # esta tardó más que el intervalo
    ritmo.esperar()
    assert reloj.esperas == [7.0]


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [(None, PAUSA_SEGUNDOS_DEFECTO), ("", PAUSA_SEGUNDOS_DEFECTO), ("4.5", 4.5),
     ("0", 0.0), ("-3", 0.0), ("abc", PAUSA_SEGUNDOS_DEFECTO)],
)
def test_pausa_configurable_por_entorno(monkeypatch, valor, esperado) -> None:
    if valor is None:
        monkeypatch.delenv("SEACE_PAUSA_SEGUNDOS", raising=False)
    else:
        monkeypatch.setenv("SEACE_PAUSA_SEGUNDOS", valor)
    assert pausa_configurada() == esperado
    assert Ritmo().intervalo == esperado


def test_cliente_de_documentos_espacia_las_peticiones() -> None:
    reloj = _Reloj()

    class _Client:
        def get(self, url):
            return SimpleNamespace(status_code=200, headers={}, content=b"x")

        def close(self):
            return None

    http = SeaceHttp(client=_Client(), sleep=lambda s: None,
                     ritmo=Ritmo(10, reloj=reloj, dormir=reloj.dormir))
    http.get_bytes("https://ejemplo/1")
    http.get_bytes("https://ejemplo/2")
    http.get_bytes("https://ejemplo/3")
    http.close()
    assert reloj.esperas == [10.0, 10.0]


@pytest.mark.parametrize(
    ("guion", "llamada"),
    [("enriquecer_detalle.py", "fetch_detalle(page, cid)"),
     ("capturar_resultado.py", "_fetch_resultado(page, cid)"),
     ("refresh_estados.py", "fetch_estado(page, cid)")],
)
def test_bucles_de_detalle_esperan_antes_de_cada_peticion(guion, llamada) -> None:
    lineas = (ROOT / guion).read_text(encoding="utf-8").splitlines()
    i = next(n for n, linea in enumerate(lineas) if llamada in linea and "def " not in linea)
    assert lineas[i - 1].strip() == "ritmo.esperar()"
    assert any(linea.strip() == "ritmo = Ritmo()" for linea in lineas[:i])


def test_modulo_expone_el_valor_por_defecto() -> None:
    assert seace_api.PAUSA_SEGUNDOS_DEFECTO == 10.0
