"""Persistencia del resultado de extracción PDF/TDR hacia ``contratos``.

Traduce la fila que produce el servicio de extracción a las columnas
``tdr_*``/``pdf_*`` y mantiene el sidecar local (``meta.registrar_meta_local``)
como respaldo reanudable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from seace_monitor.documents.meta import registrar_meta_local
from seace_monitor.documents.pdf_extraction import chars_utiles, clasificar_tipo
from seace_monitor.documents.repository import REQ_PENDIENTE_OCR, update_contrato


def _payload_storage(meta: dict) -> dict:
    payload = {
        "pdf_storage_path": meta["pdf_storage_path"],
        "pdf_storage_at": datetime.now(timezone.utc).isoformat(),
    }
    if meta.get("pdf_storage_bytes") is not None:
        payload["pdf_storage_bytes"] = int(meta["pdf_storage_bytes"])
    return payload


def guardar_ok(
    supa,
    row: dict,
    *,
    meta_path: Path | None = None,
) -> None:
    registrar_meta_local(row, path=meta_path)
    tipo = row.get("tdr_tipo_extraccion") or clasificar_tipo(
        int(row.get("n_paginas") or 0),
        list(row.get("ocr_paginas") or []),
    )
    ocr_pend = list(row.get("ocr_paginas") or [])
    ocr_hechas = list(row.get("ocr_hechas") or [])
    n_pag = int(row.get("n_paginas") or 0)
    n_ocr = int(row.get("n_paginas_ocr") if row.get("n_paginas_ocr") is not None
                else len(ocr_pend))
    n_nat = int(row.get("n_paginas_nativas") if row.get("n_paginas_nativas") is not None
                else max(n_pag - n_ocr, 0))
    payload = {
        "tdr_texto": row["tdr_texto"] or None,
        "pdf_hash": row["pdf_hash"],
        "pdf_es_imagen": tipo != "nativo_puro",
        "pdf_descargado": True,
        "pdf_procesado": tipo != "imagen_total",
        "req_url": (row["url"] or "")[:2000],
        "tdr_tipo_extraccion": tipo,
        "paginas_ocr_pendientes": ocr_pend,
        "paginas_ocr_hechas": ocr_hechas,
        "tdr_n_paginas": n_pag,
        "tdr_n_paginas_nativas": n_nat,
        "tdr_n_paginas_ocr": n_ocr,
        "_pdf_archivo_id": row.get("pdf_archivo_id"),
        "_pdf_nombre": (row.get("pdf_nombre") or "")[:500] or None,
    }
    if row.get("pdf_storage_path"):
        payload.update(_payload_storage(row))
    update_contrato(supa, row["id"], payload)


def guardar_pendiente_ocr(supa, cid: int, meta: dict) -> None:
    """Deja pdf_descargado=false para --solo-ocr. No llama a Flash."""
    payload = {
        "tdr_texto": None,
        "pdf_hash": meta.get("pdf_hash"),
        "pdf_es_imagen": True,
        "pdf_descargado": False,
        "pdf_procesado": False,
        "req_url": REQ_PENDIENTE_OCR,
        "_pdf_archivo_id": meta.get("pdf_archivo_id"),
        "_pdf_nombre": (meta.get("pdf_nombre") or "")[:500] or None,
    }
    if meta.get("pdf_storage_path"):
        payload.update(_payload_storage(meta))
    update_contrato(supa, cid, payload)


def guardar_sin_pdf(supa, cid: int) -> None:
    """No reintentar: no hay anexo PDF. tdr_texto queda null."""
    update_contrato(supa, cid, {
        "tdr_texto": None,
        "pdf_hash": None,
        "pdf_es_imagen": None,
        "pdf_descargado": True,
        "pdf_procesado": True,
        "req_url": "sin_pdf",
        "tdr_tipo_extraccion": None,
        "paginas_ocr_pendientes": [],
        "paginas_ocr_hechas": [],
        "tdr_n_paginas": None,
        "tdr_n_paginas_nativas": None,
        "tdr_n_paginas_ocr": None,
        "_pdf_archivo_id": None,
        "_pdf_nombre": None,
    })


def guardar_pdf_truncado(supa, cid: int, contrato: dict | None = None, *, meta_path=None) -> None:
    """PDF corrupto en origen: terminal — sale de pendientes_pdf y de la cola OCR."""
    contrato = contrato or {"id": cid}
    hechas = list(contrato.get("paginas_ocr_hechas") or [])
    n_pag = int(contrato.get("tdr_n_paginas") or contrato.get("n_paginas") or 0)
    n_ocr = int(contrato.get("tdr_n_paginas_ocr") or contrato.get("n_paginas_ocr") or 0)
    n_nat = int(
        contrato.get("tdr_n_paginas_nativas")
        or contrato.get("n_paginas_nativas")
        or max(n_pag - n_ocr, 0)
    )
    try:
        registrar_meta_local({
            "id": cid,
            "tdr_tipo_extraccion": contrato.get("tdr_tipo_extraccion"),
            "ocr_paginas": [],
            "ocr_hechas": hechas,
            "n_paginas": n_pag,
            "n_paginas_nativas": n_nat,
            "n_paginas_ocr": n_ocr,
            "chars_final": chars_utiles(contrato.get("tdr_texto") or ""),
        }, path=meta_path)
    except Exception as e:
        print(f"  [warn] meta local pdf_truncado id={cid}: {e}", flush=True)
    update_contrato(supa, cid, {
        "tdr_texto": None,
        "pdf_es_imagen": None,
        "pdf_descargado": True,
        "pdf_procesado": True,
        "req_url": "pdf_truncado",
        "tdr_tipo_extraccion": None,
        "paginas_ocr_pendientes": [],
        "paginas_ocr_hechas": hechas,
        "tdr_n_paginas": None,
        "tdr_n_paginas_nativas": None,
        "tdr_n_paginas_ocr": None,
        "_pdf_nombre": contrato.get("_pdf_nombre"),
        "_pdf_archivo_id": contrato.get("_pdf_archivo_id"),
    })


def persistir_storage_si_hay(supa, cid: int, meta: dict) -> None:
    """Persiste solo columnas pdf_storage_* si el upload ya llenó meta."""
    path = meta.get("pdf_storage_path")
    if not path:
        return
    payload = _payload_storage(meta)
    if meta.get("pdf_archivo_id") is not None:
        payload["_pdf_archivo_id"] = meta["pdf_archivo_id"]
    if meta.get("pdf_nombre"):
        payload["_pdf_nombre"] = str(meta["pdf_nombre"])[:500]
    update_contrato(supa, cid, payload)


def guardar_ocr_progreso(
    supa,
    contrato: dict,
    tdr: str,
    pend: list[int],
    hechas: list[int],
    *,
    meta_path: Path | None = None,
) -> None:
    """Persiste el avance página a página del OCR selectivo."""
    cid = int(contrato["id"])
    tipo = contrato.get("tdr_tipo_extraccion") or clasificar_tipo(
        int(contrato.get("tdr_n_paginas") or contrato.get("n_paginas") or 0),
        list(pend) + list(hechas),
    )
    n_pag = int(contrato.get("tdr_n_paginas") or contrato.get("n_paginas") or 0)
    n_ocr = int(
        contrato.get("n_paginas_ocr")
        or contrato.get("tdr_n_paginas_ocr")
        or (len(pend) + len(hechas))
    )
    n_nat = int(
        contrato.get("n_paginas_nativas")
        or contrato.get("tdr_n_paginas_nativas")
        or max(n_pag - n_ocr, 0)
    )
    registrar_meta_local({
        "id": cid,
        "tdr_tipo_extraccion": tipo,
        "ocr_paginas": pend,
        "ocr_hechas": hechas,
        "n_paginas": n_pag,
        "n_paginas_nativas": n_nat,
        "n_paginas_ocr": n_ocr,
        "chars_final": chars_utiles(tdr),
    }, path=meta_path)
    update_contrato(supa, cid, {
        "tdr_texto": tdr or None,
        "pdf_es_imagen": True,
        "pdf_descargado": True,
        "pdf_procesado": bool((tdr or "").strip()),
        "tdr_tipo_extraccion": tipo,
        "paginas_ocr_pendientes": pend,
        "paginas_ocr_hechas": hechas,
        "tdr_n_paginas": n_pag or None,
        "tdr_n_paginas_nativas": n_nat,
        "tdr_n_paginas_ocr": n_ocr,
        "_pdf_archivo_id": contrato.get("pdf_archivo_id"),
        "_pdf_nombre": (contrato.get("pdf_nombre") or "")[:500] or None,
    })
