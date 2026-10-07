"""Caso de uso para convertir anexos contenedores en TDR procesables."""
from __future__ import annotations

import hashlib

from seace_monitor.documents.containers import (
    MIN_CHARS_UTIL,
    MOTIVO_DESC,
    MOTIVO_DOC,
    MOTIVO_RAR,
    descargar_crudo,
    elegir_contenedor,
    extraer_docx,
    extraer_rar,
    extraer_zip,
    tipo_contenedor,
)
from seace_monitor.documents.pdf_extraction import chars_utiles
from seace_monitor.documents.repository import guardar_texto_contenedor
from seace_monitor.documents.seace_files import elegir_pdf, resumen_archivos
from seace_monitor.ingestion.repository import (
    payload_rechazo,
    registrar_rechazo,
)
from seace_monitor.logging import registrar_evento
from seace_monitor.ocr.cuota import costo_ocr
from seace_monitor.ocr.gemini_provider import (
    OCR_ACTIVO,
    OCR_USAGE_ACUM,
)


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


# ── Procesamiento por contrato ────────────────────────────────────────────────
def procesar_contenedor(
    http,
    supa,
    c: dict,
    dry_run: bool,
    *,
    listar_archivos,
    descargar_url: str,
    ocr_page=None,
    rechunk_embed=None,
) -> str:
    """Intenta extraer el TDR del contenedor no-PDF. Devuelve un estado corto."""
    cid = int(c["id"])
    # Reset del acumulador OCR para trazar solo este contrato.
    OCR_USAGE_ACUM.update({"prompt": 0, "candidates": 0, "total": 0, "llamadas": 0})
    _url, archivos = listar_archivos(http, cid)
    es_sin_pdf = (c.get("req_url") or "") == "sin_pdf"

    elegido = elegir_contenedor(archivos)
    if elegido is None:
        # ¿"PDF" disfrazado? Solo si el pipeline PDF ya lo rechazó (req_url=sin_pdf)
        # y el anexo que "parece PDF" es en realidad docx/zip/rar. Lo descargamos
        # y el tipo real se decide por magic bytes más abajo.
        pdf = elegir_pdf(archivos)
        if pdf is not None and es_sin_pdf:
            elegido = pdf
        else:
            return "tiene_pdf" if pdf is not None else "sin_contenedor"

    aid = elegido.get("idContratoArchivo")
    if not aid:
        return "sin_aid"
    dl_url = descargar_url.format(idContratoArchivo=aid, id=aid, id_archivo=aid)
    nombre = elegido.get("nombre")

    try:
        body = descargar_crudo(http, dl_url)
    except Exception as e:
        if not dry_run:
            registrar_rechazo(
                supa,
                payload_rechazo(c, str(e)[:500], {"archivos": resumen_archivos(archivos)}),
                str(e),
                origen="contenedor",
            )
        return f"descarga_fallo({str(e)[:40]})"

    tipo = tipo_contenedor(body, nombre or "")
    try:
        if tipo == "docx":
            texto, _stats = extraer_docx(body, ocr_page=ocr_page)
        elif tipo == "zip":
            texto, _stats = extraer_zip(body, ocr_page=ocr_page)
        elif tipo == "rar":
            texto, _stats = extraer_rar(body, ocr_page=ocr_page)
        elif tipo == "pdf":
            # El mime/nombre mentía; era un PDF. Lo dejamos al pipeline PDF.
            return "es_pdf"
        elif tipo == "ole2":
            if not dry_run:
                registrar_rechazo(
                    supa,
                    payload_rechazo(c, MOTIVO_DOC, {"nombre": nombre}),
                    MOTIVO_DOC,
                    origen="contenedor",
                )
            return "doc_sin_soporte"
        else:
            if not dry_run:
                registrar_rechazo(
                    supa,
                    payload_rechazo(c, MOTIVO_DESC, {"nombre": nombre, "tipo": tipo}),
                    MOTIVO_DESC,
                    origen="contenedor",
                )
            return f"tipo_{tipo}"
    except Exception as e:
        motivo = str(e)
        if "rar sin binario" in motivo:
            motivo = MOTIVO_RAR
        if not dry_run:
            registrar_rechazo(
                supa,
                payload_rechazo(c, motivo[:500], {"nombre": nombre, "tipo": tipo}),
                motivo[:500],
                origen="contenedor",
            )
        return f"extraccion_fallo({motivo[:40]})"

    if chars_utiles(texto) < MIN_CHARS_UTIL:
        if not dry_run:
            registrar_rechazo(
                supa,
                payload_rechazo(
                    c, "contenedor extraído sin texto útil",
                    {"nombre": nombre, "tipo": tipo, "chars": chars_utiles(texto)},
                ),
                "contenedor extraído sin texto útil",
                origen="contenedor",
            )
        return f"texto_vacio({chars_utiles(texto)})"

    meta = {
        "hash": _sha256(body),
        "url": dl_url,
        "aid": int(aid),
        "nombre": nombre,
        "tipo_extraccion": f"contenedor_{tipo}",
    }
    if not dry_run:
        guardar_texto_contenedor(
            supa, cid, texto, meta, min_chars_util=MIN_CHARS_UTIL
        )
        registrar_evento(
            supa,
            cid,
            "contenedor",
            chars_tdr=chars_utiles(texto),
            tipo_extraccion=f"contenedor_{tipo}",
            detalle={"nombre": nombre, "archivos": resumen_archivos(archivos)},
        )
        # OCR embebido (imágenes de DOCX/ZIP/RAR): traza a uso_ia.
        if OCR_USAGE_ACUM.get("llamadas", 0) > 0:
            costo_emb, det_costo = costo_ocr(
                int(OCR_USAGE_ACUM["prompt"]), int(OCR_USAGE_ACUM["candidates"]),
                llamadas=int(OCR_USAGE_ACUM["llamadas"]),
            )
            try:
                supa.table("uso_ia").insert({
                    "componente": "ocr",
                    "modelo": OCR_ACTIVO["modelo"],
                    "tokens_prompt": int(OCR_USAGE_ACUM["prompt"]),
                    "tokens_completion": int(OCR_USAGE_ACUM["candidates"]),
                    "tokens_total": int(OCR_USAGE_ACUM["total"]),
                    "costo_usd": costo_emb,
                    "cache_hit": False,
                    "detalle": {
                        "paginas_ocr": int(OCR_USAGE_ACUM["llamadas"]),
                        "origen": f"contenedor_{tipo}",
                        "contrato_id": cid,
                        "version_config": (
                            OCR_ACTIVO.get("version_config") or None
                        ),
                        **det_costo,
                    },
                }).execute()
            except Exception as e:
                print(f"  [warn] log_uso_ia OCR contenedor: {e}", flush=True)
        try:
            if rechunk_embed is not None:
                rechunk_embed(supa, cid)
        except Exception as e:
            print(f"  [warn] rechunk/embed id={cid}: {e}", flush=True)
    return f"ok_{tipo}({chars_utiles(texto)})"
