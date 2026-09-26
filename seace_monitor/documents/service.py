"""Orquestación de descarga, caché y extracción de un documento SEACE."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from .pdf_extraction import NecesitaOcr, PdfExtractError, extraer_paginas, pdf_sha256
from .seace_files import (
    NoEsPdf,
    SeaceHttp,
    SinPdf,
    descargar_binario,
    elegir_pdf,
    listar_archivos,
    resumen_archivos,
)
from .storage import cachear_pdf_storage

TEMP_PREFIX = "seace-tdr-"


def borrar_temp(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        print(f"  [warn] no se pudo borrar {path}: {error}", flush=True)


def procesar_contrato(
    http: SeaceHttp,
    contrato: dict,
    *,
    listar_url: str,
    descargar_url: str,
    permitir_ocr: bool = True,
    ocr_page: Callable[[bytes, str], str] | None = None,
    supa=None,
    min_chars_pagina: int = 80,
    ocr_max_paginas: int = 40,
    ocr_dpi: int = 150,
) -> dict:
    cid = int(contrato["id"])
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

    descriptor, tmp_name = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".pdf")
    os.close(descriptor)
    tmp = Path(tmp_name)
    meta = {
        "id": cid,
        "url": download_url,
        "pdf_archivo_id": int(file_id),
        "pdf_nombre": selected.get("nombre"),
        "pdf_mime": selected.get("descripcionMime"),
        "id_tipo_archivo": selected.get("idTipoArchivo"),
        "n_archivos": len(files),
        "archivos": resumen_archivos(files),
        "temp_path": str(tmp),
        "bytes": 0,
        "pdf_hash": "",
        "tdr_texto": "",
        "por_pagina": [],
        "ocr_paginas": [],
        "n_paginas": 0,
        "chars_pymupdf": 0,
        "chars_final": 0,
        "pdf_es_imagen": False,
    }
    try:
        descargar_binario(http, download_url, tmp)
        meta["pdf_hash"] = pdf_sha256(tmp)
        meta["bytes"] = tmp.stat().st_size if tmp.exists() else 0
        meta["fecha_publica"] = contrato.get("fecha_publica")
        cachear_pdf_storage(supa, contrato, meta, tmp)
        try:
            extracted = extraer_paginas(
                tmp,
                permitir_ocr=permitir_ocr,
                ocr_page=ocr_page,
                min_chars_pagina=min_chars_pagina,
                ocr_max_paginas=ocr_max_paginas,
                ocr_dpi=ocr_dpi,
            )
        except (NecesitaOcr, PdfExtractError) as error:
            error.meta = {**meta, **error.meta}
            raise
        meta.update({
            "tdr_texto": extracted["texto"],
            "pdf_es_imagen": extracted["pdf_es_imagen"],
            "tdr_tipo_extraccion": extracted["tdr_tipo_extraccion"],
            "n_paginas": extracted["n_paginas"],
            "n_paginas_nativas": extracted["n_paginas_nativas"],
            "n_paginas_ocr": extracted["n_paginas_ocr"],
            "chars_pymupdf": extracted["chars_pymupdf"],
            "chars_final": extracted["chars_final"],
            "ocr_paginas": extracted["ocr_paginas"],
            "ocr_hechas": extracted.get("ocr_hechas") or [],
            "por_pagina": extracted["por_pagina"],
        })
        return meta
    except (PdfExtractError, NecesitaOcr, NoEsPdf):
        raise
    except Exception as error:
        raise PdfExtractError(str(error), meta) from error
    finally:
        borrar_temp(tmp)
