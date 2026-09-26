"""Reportes de la extracción PDF/TDR sobre vigentes.

Conteos operativos (``conteo_pdf``), agregado por tipo de extracción
(``reporte_extraccion``/``group_by_tipo``) y el resumen de corrida que se
persiste en ``pipeline_runs``, ``data/ultima_pdf.txt`` y el step summary de
GitHub Actions.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from seace_monitor.config import RAIZ_REPO
from seace_monitor.documents.repository import PAGE_DB, REQ_PENDIENTE_OCR
from seace_monitor.logging import PASO_PDF, registrar_run
from seace_monitor.ocr.queue import aplanar_clasificacion

RESUMEN_LOG = RAIZ_REPO / "data" / "ultima_pdf.txt"


def reporte_extraccion(supa) -> dict:
    """Conteo real sobre vigentes: tipo + páginas imagen + sin_pdf."""
    nativo = mixto = imagen = sin_pdf = pendiente_ocr = 0
    pags_ocr = 0
    pags_tot = 0
    pags_nat = 0
    sin_tipo = 0
    offset = 0
    while True:
        res = (
            supa.table("contratos")
            .select(
                "tdr_tipo_extraccion,tdr_n_paginas,tdr_n_paginas_nativas,"
                "tdr_n_paginas_ocr,req_url"
            )
            .eq("estado", "Vigente")
            # PostgREST: .range() sin .order() no garantiza orden entre paginas;
            # la pagina 2 puede repetir filas de la 1 y omitir otras.
            .order("id")
            .range(offset, offset + PAGE_DB - 1)
            .execute()
        )
        batch = res.data or []
        for r in batch:
            aplanar_clasificacion(r)
            url = r.get("req_url") or ""
            if url == "sin_pdf":
                sin_pdf += 1
                continue
            if url == REQ_PENDIENTE_OCR:
                pendiente_ocr += 1
            tipo = r.get("tdr_tipo_extraccion")
            if tipo == "nativo_puro":
                nativo += 1
            elif tipo == "mixto":
                mixto += 1
            elif tipo == "imagen_total":
                imagen += 1
            else:
                sin_tipo += 1
            pags_ocr += int(r.get("tdr_n_paginas_ocr") or 0)
            pags_tot += int(r.get("tdr_n_paginas") or 0)
            pags_nat += int(r.get("tdr_n_paginas_nativas") or 0)
        if len(batch) < PAGE_DB:
            break
        offset += PAGE_DB
    return {
        "nativo_puro": nativo,
        "mixto": mixto,
        "imagen_total": imagen,
        "sin_pdf": sin_pdf,
        "pendiente_ocr_viejo": pendiente_ocr,
        "sin_tipo": sin_tipo,
        "paginas_ocr_reales": pags_ocr,
        "paginas_totales": pags_tot,
        "paginas_nativas": pags_nat,
    }


def group_by_tipo(supa, *, vigentes: bool = True) -> dict[str, int]:
    """Equivalente a SELECT tdr_tipo_extraccion, COUNT(*) GROUP BY 1."""
    out: dict[str, int] = {}
    offset = 0
    while True:
        q = supa.table("contratos").select(
            "tdr_tipo_extraccion,req_url,pdf_es_imagen"
        )
        if vigentes:
            q = q.eq("estado", "Vigente")
        # PostgREST: .range() sin .order() no garantiza orden entre paginas;
        # la pagina 2 puede repetir filas de la 1 y omitir otras.
        res = q.order("id").range(offset, offset + PAGE_DB - 1).execute()
        batch = res.data or []
        for r in batch:
            aplanar_clasificacion(r)
            tipo = r.get("tdr_tipo_extraccion")
            key = tipo if tipo else "NULL"
            out[key] = out.get(key, 0) + 1
        if len(batch) < PAGE_DB:
            break
        offset += PAGE_DB
    return out


def conteo_pdf(supa) -> dict[str, int]:
    def cnt(q):
        return q.execute().count or 0

    base = supa.table("contratos").select("id", count="exact", head=True)
    vigentes = cnt(base.eq("estado", "Vigente"))
    pend = cnt(
        supa.table("contratos").select("id", count="exact", head=True)
        .eq("estado", "Vigente")
        .or_("pdf_descargado.eq.false,pdf_descargado.is.null")
    )
    ocr_q = cnt(
        supa.table("contratos").select("id", count="exact", head=True)
        .eq("estado", "Vigente")
        .eq("req_url", REQ_PENDIENTE_OCR)
        .or_("pdf_descargado.eq.false,pdf_descargado.is.null")
    )
    ya = cnt(
        supa.table("contratos").select("id", count="exact", head=True)
        .eq("estado", "Vigente")
        .eq("pdf_descargado", True)
    )
    return {
        "vigentes": vigentes,
        "pendientes": pend,
        "pendiente_ocr": ocr_q,
        "ya_descargados": ya,
    }


def escribir_resumen(
    supa,
    stats: dict,
    *,
    log_path: Path | None = None,
) -> None:
    """Log de corrida: OCR_PAGINAS_TOTAL para vigilar el free tier de Flash."""
    log_path = log_path or RESUMEN_LOG
    registrar_run(supa, PASO_PDF, stats)
    lines = [
        f"ts={datetime.now(timezone.utc).isoformat()}",
        *[f"{k}={v}" for k, v in stats.items()],
    ]
    texto = "\n".join(lines) + "\n"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(texto, encoding="utf-8")
    except OSError as e:
        print(f"  [warn] no se pudo escribir {log_path}: {e}", flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        md = (
            "### PDF/TDR\n\n"
            f"- ok: **{stats.get('ok')}** "
            f"(nativo={stats.get('ok_nativo')}, "
            f"con OCR={stats.get('ocr_contratos')})\n"
            f"- **OCR_PAGINAS_TOTAL={stats.get('ocr_paginas_total')}** "
            "(comparte 1,500 req/día de Flash con el chat)\n"
            f"- sin_pdf: {stats.get('sin_pdf')}\n"
            f"- errores: {stats.get('err')}\n"
            f"- dry-run: {stats.get('dry_run')}  "
            f"limit: {stats.get('limit')}  "
            f"t={stats.get('elapsed_s')}s\n"
        )
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(md)
        except OSError:
            pass
    print(texto, flush=True)
