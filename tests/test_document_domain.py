"""Pruebas del dominio documental sin red, Storage ni base de datos reales."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
import pytest

from seace_monitor.documents import pdf_extraction
from seace_monitor.documents.service import procesar_contrato
from seace_monitor.documents.storage import (
    cachear_pdf_storage,
    es_ruta_arbol_tdr,
    pdf_storage_ruta,
)


def make_pdf(*texts: str) -> bytes:
    document = pymupdf.open()
    for text in texts:
        page = document.new_page()
        if text:
            page.insert_text((72, 72), text)
    raw = document.tobytes()
    document.close()
    return raw


class FakeHttp:
    def __init__(self, pdf: bytes):
        self.pdf = pdf
        self.urls: list[str] = []

    def get_bytes(self, url: str):
        self.urls.append(url)
        if "listar" in url:
            return 200, {"content-type": "application/json"}, (
                b'[{"idContratoArchivo": 91, "idTipoArchivo": 1, '
                b'"nombre": "tdr.pdf", "descripcionMime": "application/pdf"}]'
            )
        return 200, {"content-type": "application/pdf"}, self.pdf


class FakeBucket:
    def __init__(self):
        self.uploads: list[tuple] = []

    def upload(self, *args):
        self.uploads.append(args)


class FakeStorage:
    def __init__(self):
        self.bucket = FakeBucket()
        self.names: list[str] = []

    def from_(self, name: str):
        self.names.append(name)
        return self.bucket


class FakeSupabase:
    def __init__(self):
        self.storage = FakeStorage()


def test_extrae_pdf_nativo_sin_invocar_ocr(tmp_path: Path) -> None:
    path = tmp_path / "native.pdf"
    path.write_bytes(make_pdf("Texto nativo suficientemente largo para el contrato"))

    result = pdf_extraction.extraer_paginas(
        path,
        min_chars_pagina=10,
        ocr_page=lambda *_: pytest.fail("OCR no debe ejecutarse"),
    )

    assert result["tdr_tipo_extraccion"] == "nativo_puro"
    assert result["ocr_paginas"] == []
    assert "Texto nativo" in result["texto"]
    assert result["n_paginas_nativas"] == 1


def test_extrae_pdf_mixto_y_conserva_pendiente_sin_ocr(tmp_path: Path) -> None:
    path = tmp_path / "mixed.pdf"
    path.write_bytes(make_pdf("Página con texto nativo suficiente", ""))

    pending = pdf_extraction.extraer_paginas(
        path,
        permitir_ocr=False,
        min_chars_pagina=10,
    )
    completed = pdf_extraction.extraer_paginas(
        path,
        min_chars_pagina=10,
        ocr_page=lambda image, mime: "Texto recuperado por OCR",
    )

    assert pending["tdr_tipo_extraccion"] == "mixto"
    assert pending["ocr_paginas"] == [2]
    assert completed["ocr_hechas"] == [2]
    assert completed["ocr_paginas"] == []
    assert "--- pagina 2 ---\nTexto recuperado por OCR" in completed["texto"]


def test_error_ocr_incluye_metadatos_parciales(tmp_path: Path) -> None:
    path = tmp_path / "scan.pdf"
    path.write_bytes(make_pdf(""))

    with pytest.raises(pdf_extraction.PdfExtractError, match="proveedor caído") as caught:
        pdf_extraction.extraer_paginas(
            path,
            ocr_page=lambda *_: (_ for _ in ()).throw(RuntimeError("proveedor caído")),
        )

    assert caught.value.meta["n_paginas"] == 1
    assert caught.value.meta["ocr_paginas"] == [1]


def test_servicio_documental_conserva_metadatos_y_limpia_temporal() -> None:
    raw = make_pdf("Contenido contractual nativo y verificable")
    http = FakeHttp(raw)

    result = procesar_contrato(
        http,
        {"id": 42, "fecha_publica": "2026-09-25T12:00:00Z"},
        listar_url="https://seace.test/listar/{idContrato}",
        descargar_url="https://seace.test/descargar/{idContratoArchivo}",
        min_chars_pagina=10,
        ocr_page=lambda *_: pytest.fail("OCR no debe ejecutarse"),
    )

    assert result["id"] == 42
    assert result["pdf_archivo_id"] == 91
    assert result["pdf_hash"] == hashlib.sha256(raw).hexdigest()
    assert result["tdr_tipo_extraccion"] == "nativo_puro"
    assert not Path(result["temp_path"]).exists()


def test_storage_usa_mes_lima_y_sube_pdf_validado(tmp_path: Path) -> None:
    assert pdf_storage_ruta(42, 91, "2026-10-01T03:00:00Z") == "tdr/2026/09/42/91.pdf"
    assert es_ruta_arbol_tdr("tdr/2026/09/42/91.pdf")
    assert not es_ruta_arbol_tdr("42/91.pdf")

    path = tmp_path / "tdr.pdf"
    path.write_bytes(b"%PDF-1.7\ncontenido")
    supa = FakeSupabase()
    meta = {"id": 42, "pdf_archivo_id": 91}

    cachear_pdf_storage(
        supa,
        {"id": 42, "fecha_publica": datetime(2026, 9, 25, tzinfo=timezone.utc)},
        meta,
        path,
    )

    assert supa.storage.names == ["tdr"]
    upload = supa.storage.bucket.uploads[0]
    assert upload[0] == "tdr/2026/09/42/91.pdf"
    assert upload[2] == {"content-type": "application/pdf", "upsert": "true"}
    assert meta["pdf_storage_bytes"] == path.stat().st_size
