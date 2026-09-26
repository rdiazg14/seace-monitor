"""Extracción de texto PDF por página, independiente del CLI y del proveedor OCR."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from pathlib import Path

import pymupdf


class PdfExtractError(Exception):
    """Fallo de extracción con metadatos parciales recuperables."""

    def __init__(self, message: str, meta: dict):
        super().__init__(message)
        self.meta = meta


class NecesitaOcr(Exception):
    """Contrato histórico para señalar que un PDF requiere OCR."""

    def __init__(self, meta: dict):
        count = len(meta.get("ocr_paginas") or [])
        super().__init__(f"necesita OCR ({count} paginas)")
        self.meta = meta


def chars_utiles(texto: str) -> int:
    return sum(1 for char in (texto or "") if char.isalnum())


def limpiar_texto(texto: str) -> str:
    cleaned = (texto or "").replace("\x00", " ")
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def pdf_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clasificar_tipo(n_paginas: int, ocr_paginas: list[int]) -> str:
    if not ocr_paginas:
        return "nativo_puro"
    if n_paginas > 0 and len(ocr_paginas) >= n_paginas:
        return "imagen_total"
    return "mixto"


def extraer_paginas(
    path: Path,
    *,
    permitir_ocr: bool = True,
    ocr_page: Callable[[bytes, str], str] | None = None,
    min_chars_pagina: int = 80,
    ocr_max_paginas: int = 40,
    ocr_dpi: int = 150,
) -> dict:
    """Extrae texto nativo y aplica OCR solo a páginas con poco texto.

    El proveedor se recibe como callback para que este módulo no conozca claves,
    cuotas, transporte HTTP ni configuración del proceso.
    """
    paginas_txt: list[str] = []
    por_pagina: list[dict] = []
    ocr_hechas: list[int] = []
    chars_pymupdf_total = 0

    with pymupdf.open(path) as document:
        n_paginas = document.page_count
        if n_paginas == 0:
            raise RuntimeError("PDF sin paginas")

        textos_nativos: list[str] = []
        for page_number, page in enumerate(document, 1):
            native_text = (page.get_text("text") or "").strip()
            native_chars = chars_utiles(native_text)
            chars_pymupdf_total += native_chars
            textos_nativos.append(native_text)
            por_pagina.append({
                "pagina": page_number,
                "chars_pymupdf": native_chars,
                "ocr": native_chars < min_chars_pagina,
                "chars_final": native_chars,
            })

        ocr_needed = [row["pagina"] for row in por_pagina if row.get("ocr")]

        for page_number, page in enumerate(document, 1):
            native_text = textos_nativos[page_number - 1]
            native_chars = por_pagina[page_number - 1]["chars_pymupdf"]
            needs_ocr = native_chars < min_chars_pagina
            final_text = limpiar_texto(native_text)

            if needs_ocr and not permitir_ocr:
                paginas_txt.append("")
                continue
            if needs_ocr and permitir_ocr:
                if len(ocr_hechas) >= ocr_max_paginas:
                    por_pagina[page_number - 1]["omitido"] = "ocr_max"
                    por_pagina[page_number - 1]["ocr"] = False
                    paginas_txt.append(final_text)
                    continue
                if ocr_page is None:
                    raise RuntimeError("ocr_page requerido cuando permitir_ocr=True")
                pixmap = page.get_pixmap(dpi=ocr_dpi, alpha=False)
                try:
                    ocr_text = ocr_page(pixmap.tobytes("jpeg"), "image/jpeg")
                except Exception as error:
                    pending = [row["pagina"] for row in por_pagina if row.get("ocr")]
                    raise PdfExtractError(str(error), {
                        "n_paginas": n_paginas,
                        "chars_pymupdf": chars_pymupdf_total,
                        "por_pagina": por_pagina,
                        "ocr_paginas": pending,
                        "pdf_es_imagen": True,
                        "tdr_texto": "",
                        "chars_final": chars_pymupdf_total,
                    }) from error
                ocr_hechas.append(page_number)
                final_text = ocr_text or final_text
                por_pagina[page_number - 1]["chars_final"] = chars_utiles(final_text)
            paginas_txt.append(final_text)

    blocks = [
        f"--- pagina {page_number} ---\n{text}"
        for page_number, text in enumerate(paginas_txt, 1)
        if text
    ]
    text = limpiar_texto("\n\n".join(blocks))
    ocr_pending = (
        list(ocr_needed)
        if not permitir_ocr
        else [page for page in ocr_needed if page not in ocr_hechas]
    )
    extraction_type = clasificar_tipo(n_paginas, ocr_needed)
    if chars_utiles(text) == 0 and extraction_type != "imagen_total":
        raise RuntimeError("texto extraido vacio (PyMuPDF+OCR)")
    return {
        "texto": text,
        "ocr_paginas": ocr_pending,
        "ocr_hechas": ocr_hechas,
        "pdf_es_imagen": extraction_type != "nativo_puro",
        "tdr_tipo_extraccion": extraction_type,
        "n_paginas": n_paginas,
        "n_paginas_nativas": n_paginas - len(ocr_needed),
        "n_paginas_ocr": len(ocr_needed),
        "chars_pymupdf": chars_pymupdf_total,
        "chars_final": chars_utiles(text),
        "por_pagina": por_pagina,
    }
