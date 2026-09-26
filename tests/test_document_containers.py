"""Reglas y extractores de anexos contenedores sin servicios reales."""

from __future__ import annotations

import io
import zipfile

import pytest

from seace_monitor.documents.containers import (
    descargar_crudo,
    elegir_contenedor,
    extraer_docx,
    tipo_contenedor,
)
from seace_monitor.documents import container_service


def docx_bytes(text: str = "", image: bytes | None = None) -> bytes:
    buffer = io.BytesIO()
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
        f"{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
        if image is not None:
            archive.writestr("word/media/image1.png", image)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("body", "name", "expected"),
    [
        (b"  %PDF-1.7", "anexo.bin", "pdf"),
        (b"Rar!\x1a\x07\x00resto", "anexo.bin", "rar"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "anexo.doc", "ole2"),
        (b"\x89PNGresto", "anexo.bin", "imagen"),
        (b"texto", "anexo.bin", "desconocido"),
    ],
)
def test_tipo_contenedor_usa_contenido_real(
    body: bytes, name: str, expected: str
) -> None:
    assert tipo_contenedor(body, name) == expected


def test_elegir_contenedor_prioriza_docx_sobre_zip_y_rar() -> None:
    files = [
        {"nombre": "datos.rar", "id": 1},
        {"nombre": "anexos.zip", "id": 2},
        {"nombre": "tdr.docx", "id": 3},
    ]

    assert elegir_contenedor(files) == files[2]


def test_extraer_docx_prefiere_texto_nativo() -> None:
    text, stats = extraer_docx(docx_bytes("Terminos de referencia"))

    assert text == "Terminos de referencia"
    assert stats == {"nativo_chars": 20, "imagenes": 0, "ocr_img": 0}


def test_extraer_docx_inyecta_ocr_solo_para_imagenes() -> None:
    calls: list[tuple[bytes, str]] = []

    text, stats = extraer_docx(
        docx_bytes(image=b"\x89PNGimagen"),
        ocr_page=lambda body, mime: calls.append((body, mime)) or "texto OCR",
    )

    assert text == "--- pagina 1 (ocr) ---\ntexto OCR"
    assert stats == {"nativo_chars": 0, "imagenes": 1, "ocr_img": 1}
    assert calls == [(b"\x89PNGimagen", "image/png")]


class FakeHttp:
    def __init__(self, response):
        self.response = response

    def get_bytes(self, url: str):
        return self.response


def test_descarga_cruda_rechaza_html_disfrazado() -> None:
    http = FakeHttp((200, {"content-type": "application/octet-stream"}, b"<html>error"))

    with pytest.raises(RuntimeError, match="parece HTML/JSON"):
        descargar_crudo(http, "https://seace.test/anexo")


def test_servicio_persiste_y_dispara_postproceso(monkeypatch) -> None:
    body = docx_bytes("x" * 250)
    http = FakeHttp((200, {"content-type": "application/octet-stream"}, body))
    files = [{"idContratoArchivo": 77, "nombre": "tdr.docx"}]
    writes: list[tuple] = []
    events: list[tuple] = []
    postprocess: list[tuple] = []
    monkeypatch.setattr(
        container_service,
        "guardar_texto_contenedor",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )
    monkeypatch.setattr(
        container_service,
        "registrar_evento",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    result = container_service.procesar_contenedor(
        http,
        object(),
        {"id": 42, "req_url": "sin_pdf"},
        False,
        listar_archivos=lambda client, cid: ("listar", files),
        descargar_url="https://seace.test/{idContratoArchivo}",
        rechunk_embed=lambda supa, cid: postprocess.append((supa, cid)),
    )

    assert result == "ok_docx(250)"
    assert len(writes) == 1
    assert writes[0][0][1:3] == (42, "x" * 250)
    assert writes[0][1] == {"min_chars_util": 200}
    assert len(events) == 1
    assert postprocess == [(writes[0][0][0], 42)]


def test_servicio_rechaza_doc_binario_sin_escribir(monkeypatch) -> None:
    body = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    http = FakeHttp((200, {"content-type": "application/octet-stream"}, body))
    rejects: list[tuple] = []
    monkeypatch.setattr(
        container_service,
        "registrar_rechazo",
        lambda *args, **kwargs: rejects.append((args, kwargs)),
    )

    result = container_service.procesar_contenedor(
        http,
        object(),
        {"id": 42, "req_url": "sin_pdf"},
        False,
        listar_archivos=lambda client, cid: (
            "listar",
            [{"idContratoArchivo": 77, "nombre": "tdr.doc"}],
        ),
        descargar_url="https://seace.test/{idContratoArchivo}",
    )

    assert result == "doc_sin_soporte"
    assert len(rejects) == 1
    assert rejects[0][1] == {"origen": "contenedor"}
