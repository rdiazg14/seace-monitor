"""Contrato del cliente y selección de anexos SEACE sin red real."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import descargar_requerimiento as entrypoint
from seace_monitor.documents import seace_files


class FakeHttp:
    def __init__(self, result: tuple[int, dict[str, str], bytes]):
        self.result = result
        self.urls: list[str] = []

    def get_bytes(self, url: str) -> tuple[int, dict[str, str], bytes]:
        self.urls.append(url)
        return self.result


class FakeLowClient:
    def __init__(self, responses: list[SimpleNamespace]):
        self.responses = iter(responses)
        self.urls: list[str] = []
        self.closed = False

    def get(self, url: str) -> SimpleNamespace:
        self.urls.append(url)
        return next(self.responses)

    def close(self) -> None:
        self.closed = True


def low_response(status: int, body: bytes = b"", **headers) -> SimpleNamespace:
    return SimpleNamespace(status_code=status, content=body, headers=headers)


def test_elegir_pdf_acepta_senales_imperfectas_y_prioriza_tipo_principal() -> None:
    archivos = [
        {"idContratoArchivo": 1, "nombre": "anexo.txt", "descripcionMime": "text/plain"},
        {
            "idContratoArchivo": 2,
            "idTipoArchivo": 2,
            "nombre": "alternativo.pdf",
            "descripcionMime": "application/octet-stream",
        },
        {
            "idContratoArchivo": 3,
            "idTipoArchivo": 1,
            "nombre": "principal",
            "descripcionExtension": ".PDF",
        },
    ]

    assert seace_files.elegir_pdf(archivos) is archivos[2]
    assert seace_files.elegir_pdf([archivos[0], "basura"]) is None
    assert seace_files.resumen_archivos([archivos[2], "basura"]) == [{
        "idContratoArchivo": 3,
        "idTipoArchivo": 1,
        "nombre": "principal",
        "descripcionMime": None,
    }]


def test_validacion_binaria_prioriza_magic_y_detecta_html() -> None:
    assert seace_files.es_pdf(b"  %PDF-1.7 contenido", "application/octet-stream")
    assert seace_files.es_pdf(b"sin magic", "application/pdf; charset=binary")
    assert not seace_files.es_pdf(b"PK archivo", "application/octet-stream")
    assert seace_files.parece_html(b"  <!DOCTYPE html><html></html>")
    assert not seace_files.parece_html(b"%PDF-1.7")


def test_listar_archivos_extrae_lista_de_respuesta_envuelta() -> None:
    rows = [{"idContratoArchivo": 9}]
    http = FakeHttp((200, {}, json.dumps({"data": rows}).encode()))

    url, result = seace_files.listar_archivos(
        http,
        42,
        "https://seace.test/{idContrato}/{id}/{id_contrato}",
    )

    assert url == "https://seace.test/42/42/42"
    assert result == rows
    assert http.urls == [url]


@pytest.mark.parametrize(
    "result, message",
    [
        ((503, {}, b""), "listar HTTP 503"),
        ((200, {}, b"no-json"), "listar JSON invalido"),
        ((200, {}, b'{"data": {}}'), "listar no es lista"),
    ],
)
def test_listar_archivos_rechaza_respuestas_invalidas(result, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        seace_files.listar_archivos(FakeHttp(result), 42)


def test_descargar_binario_escribe_pdf_validado(tmp_path) -> None:
    body = b"%PDF-1.7\ncontenido"
    destination = tmp_path / "tdr.pdf"
    http = FakeHttp((200, {"content-type": "application/octet-stream"}, body))

    seace_files.descargar_binario(http, "https://seace.test/file", destination)

    assert destination.read_bytes() == body


@pytest.mark.parametrize(
    "result, error_type, message",
    [
        ((404, {"content-type": "text/plain"}, b"missing"), RuntimeError, "descargar HTTP 404"),
        ((200, {}, b""), RuntimeError, "respuesta vacia"),
        ((200, {"content-type": "text/html"}, b"<html>error</html>"), seace_files.NoEsPdf, "no es PDF"),
        ((200, {"content-type": "application/json"}, b"{}"), seace_files.NoEsPdf, "no es PDF"),
        ((200, {"content-type": "application/octet-stream"}, b"PK zip"), seace_files.NoEsPdf, "binario no es PDF"),
    ],
)
def test_descargar_binario_rechaza_respuestas_no_pdf(
    tmp_path,
    result,
    error_type,
    message: str,
) -> None:
    destination = tmp_path / "tdr.pdf"

    with pytest.raises(error_type, match=message):
        seace_files.descargar_binario(
            FakeHttp(result),
            "https://seace.test/file",
            destination,
        )

    assert not destination.exists()


def test_cliente_http_reintenta_5xx_y_normaliza_headers() -> None:
    sleeps: list[float] = []
    low_client = FakeLowClient([
        low_response(503),
        low_response(200, b"ok", **{"Content-Type": "application/pdf"}),
    ])
    client = seace_files.SeaceHttp(client=low_client, sleep=sleeps.append)

    assert client.get_bytes("https://seace.test/file") == (
        200,
        {"content-type": "application/pdf"},
        b"ok",
    )
    assert sleeps == [1.0]
    assert low_client.urls == ["https://seace.test/file"] * 2
    client.close()
    assert low_client.closed


def test_cliente_http_deriva_403_a_playwright(monkeypatch) -> None:
    low_client = FakeLowClient([low_response(403)])
    client = seace_files.SeaceHttp(client=low_client, sleep=lambda seconds: None)
    fallback = (200, {"content-type": "application/pdf"}, b"%PDF")
    calls: list[str] = []
    monkeypatch.setattr(
        client,
        "_get_pw",
        lambda url: calls.append(url) or fallback,
    )

    assert client.get_bytes("https://seace.test/file") == fallback
    assert calls == ["https://seace.test/file"]


def test_entrypoint_conserva_exports_y_plantilla_configurable(monkeypatch) -> None:
    rows = [{"idContratoArchivo": 5}]
    http = FakeHttp((200, {}, json.dumps(rows).encode()))
    monkeypatch.setattr(entrypoint, "LISTAR_URL", "https://custom.test/{idContrato}")

    assert entrypoint.listar_archivos(http, 77) == (
        "https://custom.test/77",
        rows,
    )
    assert entrypoint.SeaceHttp is seace_files.SeaceHttp
    assert entrypoint.SinPdf is seace_files.SinPdf
    assert entrypoint.NoEsPdf is seace_files.NoEsPdf
    assert entrypoint.elegir_pdf is seace_files.elegir_pdf
    assert entrypoint.descargar_binario is seace_files.descargar_binario
