"""Reglas de cola y servicio OCR con dependencias locales."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pymupdf

from seace_monitor.ocr.queue import (
    filtrar_ordenar_cola_ocr,
    ocr_sin_margen_contrato,
    ocr_tiempo_agotado,
)
from seace_monitor.ocr.service import procesar_paginas_pendientes


def blank_pdf() -> bytes:
    document = pymupdf.open()
    document.new_page()
    raw = document.tobytes()
    document.close()
    return raw


class FakeHttp:
    def __init__(self, pdf: bytes):
        self.pdf = pdf

    def get_bytes(self, url: str):
        if "listar" in url:
            return 200, {"content-type": "application/json"}, (
                b'[{"idContratoArchivo": 7, "idTipoArchivo": 1, '
                b'"nombre": "scan.pdf"}]'
            )
        return 200, {"content-type": "application/pdf"}, self.pdf


def test_cola_filtra_ventana_y_prioriza_ti() -> None:
    now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
    future = (now + timedelta(days=1)).isoformat()
    past = (now - timedelta(days=1)).isoformat()
    rows = [
        {"id": 1, "estado": "Vigente", "fecha_fin_cotizacion": future, "relevancia_ia": "MEDIA"},
        {"id": 2, "estado": "Vigente", "fecha_fin_cotizacion": future, "categoria_it": "software"},
        {"id": 3, "estado": "Vigente", "fecha_fin_cotizacion": future, "relevancia_ia": "ALTA"},
        {"id": 4, "estado": "Vigente", "fecha_fin_cotizacion": past, "relevancia_ia": "ALTA"},
        {"id": 5, "estado": "Vigente", "fecha_fin_cotizacion": None, "relevancia_ia": "ALTA"},
        {"id": 6, "estado": "Cancelado", "fecha_fin_cotizacion": future, "relevancia_ia": "ALTA"},
        {"id": 7, "estado": "Vigente", "fecha_fin_cotizacion": future},
    ]

    selected, stats = filtrar_ordenar_cola_ocr(rows, solo_ti=True, now=now)

    assert [row["id"] for row in selected] == [3, 2, 1]
    assert stats == {
        "crudos": 7, "no_vigente": 1, "ventana_null": 1,
        "vencidos": 1, "por_abrir": 0, "no_ti": 1, "ok": 3,
        "alta": 1, "categoria_it": 1, "media": 1, "baja": 0,
    }


def test_limites_de_reloj_son_deterministas() -> None:
    assert ocr_tiempo_agotado(100, 20, clock=lambda: 120)
    assert not ocr_tiempo_agotado(100, 0, clock=lambda: 999)
    assert ocr_sin_margen_contrato(100, 60, clock=lambda: 116, min_segundos=45)
    assert not ocr_sin_margen_contrato(100, 60, clock=lambda: 115, min_segundos=45)


def test_servicio_ocr_persiste_cada_pagina_y_actualiza_cuota() -> None:
    saved: list[tuple] = []
    storage: list[tuple] = []
    quota = {"requests": 0}

    def save_progress(supa, contract, text, pending, completed):
        saved.append((text, list(pending), list(completed)))

    def register_success(supa, current_quota, max_day):
        current_quota["requests"] += 1

    result = procesar_paginas_pendientes(
        FakeHttp(blank_pdf()),
        None,
        {
            "id": 42,
            "paginas_ocr_pendientes": [1, 99],
            "paginas_ocr_hechas": [],
            "tdr_texto": "Texto nativo",
        },
        quota,
        5,
        t0=0,
        max_segundos=0,
        listar_url="https://seace.test/listar/{idContrato}",
        descargar_url="https://seace.test/descargar/{idContratoArchivo}",
        ocr_page=lambda image, mime: "Texto OCR",
        append_ocr=lambda base, page, text: f"{base}\n[OCR {page}] {text}",
        save_progress=save_progress,
        register_success=register_success,
        persist_storage=lambda supa, cid, meta: storage.append((cid, meta)),
    )

    assert result == {
        "id": 42,
        "nuevas": [1],
        "pend": [],
        "hechas": [1],
        "tdr_chars": len("Texto nativo\n[OCR 1] Texto OCR"),
    }
    assert saved == [("Texto nativo\n[OCR 1] Texto OCR", [99], [1])]
    assert quota["requests"] == 1
    assert storage == [(42, {
        "id": 42,
        "pdf_archivo_id": 7,
        "pdf_nombre": "scan.pdf",
        "fecha_publica": None,
    })]
