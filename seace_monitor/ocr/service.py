"""Servicio de OCR selectivo por páginas pendientes."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path

import pymupdf

from seace_monitor.documents.seace_files import (
    SeaceHttp,
    SinPdf,
    descargar_binario,
    elegir_pdf,
    listar_archivos,
)
from seace_monitor.documents.service import borrar_temp
from seace_monitor.documents.storage import cachear_pdf_storage

from .gemini_provider import CupoFlash
from .queue import ocr_tiempo_agotado


def procesar_paginas_pendientes(
    http: SeaceHttp,
    supa,
    contrato: dict,
    cuota: dict,
    max_dia: int,
    *,
    t0: float,
    max_segundos: int,
    listar_url: str,
    descargar_url: str,
    ocr_page: Callable[[bytes, str], str],
    append_ocr: Callable[[str, int, str], str],
    save_progress: Callable[[object, dict, str, list[int], list[int]], None],
    register_success: Callable[[object, dict, int], None],
    persist_storage: Callable[[object, int, dict], None],
    ocr_dpi: int = 150,
    temp_prefix: str = "seace-tdr-",
) -> dict:
    """Añade OCR página por página y persiste progreso tras cada éxito."""
    cid = int(contrato["id"])
    pending = list(contrato.get("paginas_ocr_pendientes") or [])
    completed = list(contrato.get("paginas_ocr_hechas") or [])
    tdr_text = contrato.get("tdr_texto") or ""
    new_pages: list[int] = []

    _list_url, files = listar_archivos(http, cid, listar_url)
    selected = elegir_pdf(files)
    if selected is None:
        raise SinPdf(files)
    file_id = selected.get("idContratoArchivo")
    if not file_id:
        raise RuntimeError("PDF sin idContratoArchivo")
    download_url = descargar_url.format(
        idContratoArchivo=file_id,
        id=file_id,
        id_archivo=file_id,
    )
    contrato["pdf_archivo_id"] = int(file_id)
    contrato["pdf_nombre"] = selected.get("nombre")

    descriptor, tmp_name = tempfile.mkstemp(prefix=temp_prefix, suffix=".pdf")
    os.close(descriptor)
    tmp = Path(tmp_name)
    try:
        descargar_binario(http, download_url, tmp)
        if not contrato.get("pdf_storage_path"):
            storage_meta = {
                "id": cid,
                "pdf_archivo_id": contrato.get("pdf_archivo_id"),
                "pdf_nombre": contrato.get("pdf_nombre"),
                "fecha_publica": contrato.get("fecha_publica"),
            }
            cachear_pdf_storage(supa, contrato, storage_meta, tmp)
            persist_storage(supa, cid, storage_meta)
        with pymupdf.open(tmp) as document:
            page_count = document.page_count
            if not contrato.get("tdr_n_paginas"):
                contrato["tdr_n_paginas"] = page_count
            for page_number in list(pending):
                if page_number < 1 or page_number > page_count:
                    pending.remove(page_number)
                    continue
                if ocr_tiempo_agotado(t0, max_segundos):
                    raise CupoFlash(f"tope {max_segundos}s de reloj", motivo="tiempo")
                if int(cuota.get("requests") or 0) >= max_dia:
                    raise CupoFlash(
                        f"tope diario {max_dia} Flash (usadas={cuota['requests']})",
                        motivo="cupo",
                    )
                pixmap = document[page_number - 1].get_pixmap(dpi=ocr_dpi, alpha=False)
                page_text = ocr_page(pixmap.tobytes("jpeg"), "image/jpeg")
                tdr_text = append_ocr(tdr_text, page_number, page_text)
                pending.remove(page_number)
                if page_number not in completed:
                    completed.append(page_number)
                new_pages.append(page_number)
                save_progress(supa, contrato, tdr_text, pending, completed)
                register_success(supa, cuota, max_dia)
    finally:
        borrar_temp(tmp)

    return {
        "id": cid,
        "nuevas": new_pages,
        "pend": pending,
        "hechas": completed,
        "tdr_chars": len(tdr_text or ""),
    }
