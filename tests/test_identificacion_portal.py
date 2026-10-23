"""El proyecto se identifica con un nombre propio en todas las peticiones al portal.

Desde el 18/10/2026 el portal rechaza con 403 los nombres por defecto de las
herramientas (python-httpx, HeadlessChrome) y los que contienen ciertas
palabras; acepta un nombre que dice quién consulta y que es automatizado.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from seace_monitor.documents.seace_files import SeaceHttp
from seace_monitor.seace_api import IDENTIFICACION

ROOT = Path(__file__).resolve().parent.parent

# Rechazadas por el portal según las pruebas del 23/10/2026.
PALABRAS_RECHAZADAS = ("python", "httpx", "curl", "headlesschrome", "bot",
                       "monitor", "seace")


@pytest.mark.parametrize("palabra", PALABRAS_RECHAZADAS)
def test_identificacion_sin_palabras_rechazadas(palabra: str) -> None:
    assert palabra not in IDENTIFICACION.lower()


def test_identificacion_declara_consulta_automatizada() -> None:
    assert IDENTIFICACION.startswith("Enertronic/")
    assert "consulta automatizada" in IDENTIFICACION


def test_cliente_de_documentos_envia_la_identificacion() -> None:
    http = SeaceHttp()
    try:
        assert http._client.headers["user-agent"] == IDENTIFICACION
    finally:
        http.close()


def test_respaldo_playwright_usa_la_identificacion() -> None:
    contextos: list[dict] = []

    class _Page:
        def goto(self, *args, **kwargs):
            return None

        def wait_for_timeout(self, *args):
            return None

    class _Context:
        def new_page(self):
            return _Page()

    class _Browser:
        def new_context(self, **kwargs):
            contextos.append(kwargs)
            return _Context()

        def close(self):
            return None

    class _Chromium:
        def launch(self, **kwargs):
            return _Browser()

    class _Pw:
        chromium = _Chromium()

        def start(self):
            return self

        def stop(self):
            return None

    http = SeaceHttp(playwright_factory=lambda: _Pw())
    try:
        http._ensure_pw()
    finally:
        http.close()
    assert contextos == [{"ignore_https_errors": True,
                          "user_agent": IDENTIFICACION}]


def _fuentes_que_abren_navegador() -> list[Path]:
    rutas = list(ROOT.glob("*.py")) + list((ROOT / "seace_monitor").rglob("*.py"))
    return sorted(p for p in rutas if "new_context(" in p.read_text(encoding="utf-8"))


def test_todo_contexto_de_navegador_se_identifica() -> None:
    fuentes = _fuentes_que_abren_navegador()
    assert fuentes, "no se encontraron guiones con navegador"
    sin_nombre = [
        f"{p.name}: {linea.strip()}"
        for p in fuentes
        for linea in p.read_text(encoding="utf-8").splitlines()
        if re.search(r"\bnew_context\(", linea)
        and "user_agent=IDENTIFICACION" not in linea
    ]
    assert sin_nombre == []
