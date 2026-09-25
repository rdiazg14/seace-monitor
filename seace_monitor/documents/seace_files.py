"""Cliente y reglas de selección/validación de anexos públicos de SEACE."""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

from seace_monitor.seace_api import SPA_URL


DEFAULT_LISTAR_URL = (
    "https://prod6.seace.gob.pe/v1/s8uit-services/archivo"
    "/archivos-publico/listar-archivos-contrato/{idContrato}/1"
)
DEFAULT_DESCARGAR_URL = (
    "https://prod6.seace.gob.pe/v1/s8uit-services/archivo"
    "/archivos-publico/descargar-archivo-contrato/{idContratoArchivo}"
)
MOTIVO_SIN_PDF = "sin archivo PDF"
MOTIVO_NO_PDF = "archivo no es PDF"


class SinPdf(Exception):
    """El listado no contiene un anexo candidato a PDF."""

    def __init__(self, archivos: list):
        super().__init__(MOTIVO_SIN_PDF)
        self.archivos = archivos or []


class NoEsPdf(Exception):
    """El anexo seleccionado no contiene un PDF real."""

    def __init__(self, message: str):
        super().__init__(f"{MOTIVO_NO_PDF} ({message})")


class SeaceHttp:
    """GET con httpx y fallback Playwright únicamente ante 401/403."""

    def __init__(
        self,
        headed: bool = False,
        *,
        client=None,
        playwright_factory=sync_playwright,
        sleep: Callable[[float], None] = time.sleep,
        spa_url: str = SPA_URL,
    ):
        self.headed = headed
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
            headers={"Accept": "*/*"},
        )
        self._playwright_factory = playwright_factory
        self._sleep = sleep
        self._spa_url = spa_url
        self._pw = None
        self._browser = None
        self._page = None

    def get_bytes(self, url: str) -> tuple[int, dict[str, str], bytes]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self._client.get(url)
                if response.status_code in (401, 403):
                    print(
                        f"  [fallback] Playwright por HTTP {response.status_code}",
                        flush=True,
                    )
                    return self._get_pw(url)
                if response.status_code >= 500:
                    last_error = RuntimeError(f"HTTP {response.status_code}")
                    self._sleep(1.0 * (attempt + 1))
                    continue
                headers = {key.lower(): value for key, value in response.headers.items()}
                return response.status_code, headers, response.content or b""
            except httpx.HTTPError as error:
                last_error = error
                self._sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"GET fallo: {last_error}")

    def _get_pw(self, url: str) -> tuple[int, dict[str, str], bytes]:
        page = self._ensure_pw()
        response = page.request.get(url, timeout=60_000)
        headers = {key.lower(): value for key, value in response.headers.items()}
        return response.status, headers, response.body() or b""

    def _ensure_pw(self):
        if self._page is not None:
            return self._page
        self._pw = self._playwright_factory().start()
        self._browser = self._pw.chromium.launch(headless=not self.headed)
        self._page = self._browser.new_context(ignore_https_errors=True).new_page()
        self._page.goto(self._spa_url, wait_until="networkidle", timeout=90_000)
        self._page.wait_for_timeout(2_000)
        return self._page

    def close(self) -> None:
        self._client.close()
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._pw is not None:
                self._pw.stop()


def es_pdf(body: bytes, content_type: str) -> bool:
    head = body[:16].lstrip()
    if head.startswith(b"%PDF"):
        return True
    normalized = (content_type or "").lower()
    return "application/pdf" in normalized or normalized.endswith("/pdf")


def parece_html(body: bytes) -> bool:
    sample = body[:400].lstrip().lower()
    return (
        sample.startswith(b"<!doctype")
        or sample.startswith(b"<html")
        or b"<html" in sample[:200]
    )


def resumen_archivos(archivos: list) -> list[dict]:
    return [
        {
            "idContratoArchivo": item.get("idContratoArchivo"),
            "idTipoArchivo": item.get("idTipoArchivo"),
            "nombre": item.get("nombre"),
            "descripcionMime": item.get("descripcionMime"),
        }
        for item in archivos
        if isinstance(item, dict)
    ]


def candidato_pdf(archivo: dict) -> bool:
    """Usa el listado solo como señal; el binario sigue siendo la autoridad."""
    mime = str(archivo.get("descripcionMime") or "").lower()
    if "pdf" in mime:
        return True
    extension = str(archivo.get("descripcionExtension") or "").strip().lower()
    if extension == ".pdf":
        return True
    nombre = str(archivo.get("nombre") or "").strip().lower()
    return nombre.endswith(".pdf")


def elegir_pdf(archivos: list) -> dict | None:
    candidatos = [
        item
        for item in archivos
        if isinstance(item, dict) and candidato_pdf(item)
    ]
    if not candidatos:
        return None
    tipo_principal = [item for item in candidatos if item.get("idTipoArchivo") == 1]
    return (tipo_principal or candidatos)[0]


def listar_archivos(
    http: SeaceHttp,
    contrato_id: int,
    url_template: str = DEFAULT_LISTAR_URL,
) -> tuple[str, list]:
    url = url_template.format(
        idContrato=contrato_id,
        id=contrato_id,
        id_contrato=contrato_id,
    )
    status, _headers, body = http.get_bytes(url)
    if status != 200:
        raise RuntimeError(f"listar HTTP {status}")
    try:
        data = json.loads(body)
    except Exception as error:
        raise RuntimeError(f"listar JSON invalido: {error}") from error
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                data = value
                break
    if not isinstance(data, list):
        raise RuntimeError(f"listar no es lista ({type(data).__name__})")
    return url, data


def descargar_binario(http: SeaceHttp, url: str, destination: Path) -> None:
    status, headers, body = http.get_bytes(url)
    content_type = headers.get("content-type") or ""
    if status != 200:
        raise RuntimeError(f"descargar HTTP {status} ({content_type[:80]})")
    if not body:
        raise RuntimeError("respuesta vacia al descargar PDF")
    if parece_html(body) or "json" in content_type.lower() or "text/html" in content_type.lower():
        raise NoEsPdf(
            f"no es PDF (content-type={content_type[:80]} n={len(body)})"
        )
    if not es_pdf(body, content_type):
        raise NoEsPdf(
            f"binario no es PDF (content-type={content_type[:80]} n={len(body)} "
            f"magic={body[:8]!r})"
        )
    destination.write_bytes(body)


# Alias privados para conservar imports históricos del entrypoint.
_es_pdf = es_pdf
_parece_html = parece_html
_candidato_pdf = candidato_pdf
