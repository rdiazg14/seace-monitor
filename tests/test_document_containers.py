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


# ── RAR (OPS-001): tool global correcto y motivo honesto ──────────────────────

def _fake_rarfile():
    import sys
    import types

    fake = types.SimpleNamespace(
        UNRAR_TOOL="unrar",
        SEVENZIP_TOOL="7z",
        SEVENZIP2_TOOL="7zz",
        BSDTAR_TOOL="bsdtar",
        UNAR_TOOL="unar",
    )

    class FakeRar:
        def __init__(self, path):
            pass

        def infolist(self):
            return [types.SimpleNamespace(
                is_dir=lambda: False, filename="t.txt")]

        def read(self, name):
            return b"texto"

    fake.RarFile = FakeRar
    return fake


def test_extraer_rar_7z_asigna_sevenzip_no_unrar(monkeypatch) -> None:
    # OPS-001: UNRAR_TOOL='7z' ejecutaba 7z con sintaxis unrar y fallaba.
    import sys
    import seace_monitor.documents.containers as containers

    fake = _fake_rarfile()
    monkeypatch.setitem(sys.modules, "rarfile", fake)
    monkeypatch.setattr(containers, "_find_rar_tool", lambda: "7z")

    containers.extraer_rar(b"Rar!\x1a\x07\x00fake")

    assert fake.SEVENZIP_TOOL == "7z"
    assert fake.UNRAR_TOOL == "unrar"  # no pisado


def test_extraer_rar_7zz_asigna_sevenzip2(monkeypatch) -> None:
    import sys
    import seace_monitor.documents.containers as containers

    fake = _fake_rarfile()
    monkeypatch.setitem(sys.modules, "rarfile", fake)
    monkeypatch.setattr(containers, "_find_rar_tool", lambda: "7zz")

    containers.extraer_rar(b"Rar!\x1a\x07\x00fake")

    assert fake.SEVENZIP2_TOOL == "7zz"


def test_servicio_rar_error_real_no_se_enmascara(monkeypatch) -> None:
    # OPS-001: antes todo fallo RAR se re-etiquetaba 'sin binario' aunque el
    # binario existiera y la causa fuera otra (codec ausente, archivo dañado).
    body = b"Rar!\x1a\x07\x00resto"
    http = FakeHttp((200, {"content-type": "application/octet-stream"}, body))
    rejects: list = []
    monkeypatch.setattr(
        container_service,
        "registrar_rechazo",
        lambda *args, **kwargs: rejects.append((args, kwargs)),
    )
    monkeypatch.setattr(
        container_service,
        "extraer_rar",
        lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("rar con 120 archivos (> 100)")),
    )

    result = container_service.procesar_contenedor(
        http,
        object(),
        {"id": 42, "req_url": "sin_pdf"},
        False,
        listar_archivos=lambda client, cid: (
            "listar",
            [{"idContratoArchivo": 77, "nombre": "REQUERIMIENTO.rar"}],
        ),
        descargar_url="https://seace.test/{idContratoArchivo}",
    )

    assert "extraccion_fallo" in result
    assert rejects and rejects[0][0][2] != container_service.MOTIVO_RAR
    assert "120 archivos" in rejects[0][0][2]


def test_servicio_rar_sin_binario_se_normaliza(monkeypatch) -> None:
    body = b"Rar!\x1a\x07\x00resto"
    http = FakeHttp((200, {"content-type": "application/octet-stream"}, body))
    rejects: list = []
    monkeypatch.setattr(
        container_service,
        "registrar_rechazo",
        lambda *args, **kwargs: rejects.append((args, kwargs)),
    )
    monkeypatch.setattr(
        container_service,
        "extraer_rar",
        lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError(container_service.MOTIVO_RAR)),
    )

    result = container_service.procesar_contenedor(
        http,
        object(),
        {"id": 42, "req_url": "sin_pdf"},
        False,
        listar_archivos=lambda client, cid: (
            "listar",
            [{"idContratoArchivo": 77, "nombre": "REQUERIMIENTO.rar"}],
        ),
        descargar_url="https://seace.test/{idContratoArchivo}",
    )

    assert "extraccion_fallo" in result
    assert rejects[0][0][2] == container_service.MOTIVO_RAR
