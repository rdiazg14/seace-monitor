#!/usr/bin/env python3
"""
Fase 3 — Descarga y extracción de PDF/TDR (solo vigentes).

Idempotente: SELECT estado='Vigente' AND pdf_descargado=false.

Flujo por contrato (2 GET, capturados del SEACE):
  1) LISTAR_URL  → JSON de anexos
  2) elige application/pdf (prioriza idTipoArchivo=1)
  3) DESCARGAR_URL → binario
  4) PyMuPDF por página → tdr_texto nativo; páginas <80 chars quedan
     pendientes de OCR (no se manda el PDF entero a Flash).
     tipo: nativo_puro | mixto | imagen_total.

httpx directo. Playwright solo si alguna GET da 401/403.

Este archivo compone configuración, wiring y comandos. Las reglas y la
persistencia viven en el paquete: documents.meta (sidecar/columnas/sync),
documents.persistencia (escrituras de extracción), documents.reportes,
documents.pdf_extraction (formato TDR), ocr.cuota (tope diario Flash),
ocr.pendientes (cola), ocr.selectivo (corrida Paso C). Los nombres históricos
se re-exportan para conservar a los consumidores del entrypoint.

Uso:
  uv run python descargar_requerimiento.py --dry-run --limit 5
  uv run python descargar_requerimiento.py --limit 20
  uv run python descargar_requerimiento.py --solo-nativo --limit 0
  uv run python descargar_requerimiento.py --reporte
  uv run python descargar_requerimiento.py --sync-meta
  uv run python descargar_requerimiento.py --solo-ocr --rpm 8 --limit 50
  uv run python descargar_requerimiento.py --solo-ocr --solo-ti --max-segundos 7200
  uv run python descargar_requerimiento.py --solo-ocr --solo-ti --incluir-por-abrir
"""
from __future__ import annotations

from seace_monitor.config import cargar_env
from seace_monitor.documents.seace_files import (
    DEFAULT_DESCARGAR_URL,
    DEFAULT_LISTAR_URL,
    MOTIVO_NO_PDF,
    MOTIVO_SIN_PDF,
    SPA_URL,
    NoEsPdf,
    SeaceHttp,
    SinPdf,
    _candidato_pdf,
    _es_pdf,
    _parece_html,
    descargar_binario,
    elegir_pdf,
    listar_archivos as listar_archivos_seace,
    resumen_archivos,
)
from seace_monitor.documents.pdf_extraction import (
    NecesitaOcr,
    PdfExtractError,
    anexar_ocr_a_tdr,
    chars_utiles,
    clasificar_tipo,
    extraer_paginas as extraer_paginas_documento,
    limpiar_texto,
    pdf_sha256,
)
from seace_monitor.documents.repository import (
    COLS_EXTRACCION,
    PAGE_DB,
    REQ_PENDIENTE_OCR,
    contratos_por_ids,
    pendientes_pdf,
    update_contrato as actualizar_contrato_documental,
)
from seace_monitor.documents.meta import (
    META_LOG,
    columnas_extraccion_ok,
    meta_local_por_id,
    payload_extraccion,
    registrar_meta_local,
    reporte_jsonl,
    sync_meta_jsonl,
    update_extraccion as _update_extraccion,
)
from seace_monitor.documents.persistencia import (
    guardar_ok,
    guardar_ocr_progreso,
    guardar_pendiente_ocr,
    guardar_sin_pdf,
    persistir_storage_si_hay,
)
from seace_monitor.documents.reportes import (
    conteo_pdf,
    escribir_resumen,
    group_by_tipo,
    reporte_extraccion,
)
from seace_monitor.documents.postprocess import rechunk_embed_pdf as _rechunk_embed_pdf
from seace_monitor.documents.batch import ejecutar_descarga_batch
from seace_monitor.documents.service import (
    borrar_temp,
    procesar_contrato as procesar_documento,
)
from seace_monitor.documents.storage import (
    BUCKET_TDR,
    MAX_PDF_STORAGE_BYTES,
    cachear_pdf_storage,
    es_ruta_arbol_tdr,
    pdf_storage_ruta,
)
from seace_monitor.gemini import (
    FLASH_USD_IN_PER_M,
    FLASH_USD_OUT_PER_M,
    fecha_lima,
    usd_flash as usd_de_tokens,
)
from seace_monitor.ocr.cuota import (
    CUOTA_OCR_PATH,
    CUOTA_OCR_TABLA,
    cargar_cuota_ocr,
    guardar_cuota_ocr,
    registrar_ocr_ok,
)
from seace_monitor.ia.pipeline import (
    cfg_con_credencial,
    cfg_resuelta,
    embeddings_configurados,
    extras_embeddings,
    fijar_ocr_activo,
    solicitar_ocr_cfg,
)
from seace_monitor.ocr.contingencia import ocr_con_contingencia
from seace_monitor.ocr.gemini_provider import (
    GEMINI_FLASH,
    LAST_OCR_USAGE,
    OCR_USAGE_ACUM,
    CupoFlash,
    solicitar_ocr_gemini,
)
from seace_monitor.ocr.pendientes import (
    _as_int_list,
    contrato_ocr_sigue_elegible,
    pendientes_ocr_paginas,
)
from seace_monitor.ocr.queue import (
    aplanar_clasificacion as _aplanar_cl,
    es_ti,
    filtrar_ordenar_cola_ocr,
    ocr_sin_margen_contrato,
    ocr_tiempo_agotado,
    parse_datetime as _parse_dt,
    prio_ti,
    ventana_cotizacion_abierta,
)
from seace_monitor.ocr.selectivo import (
    OCR_LOG,
    OCR_MAX_SEGUNDOS_DEFAULT,
    USD_PEN,
    escribir_ocr_log,
    run_ocr_selectivo,
)
from seace_monitor.ocr.service import procesar_paginas_pendientes

import argparse
import os
import re
import tempfile
import time
from pathlib import Path

import httpx
from seace_monitor.supabase_client import crear_cliente

from seace_monitor.ingestion.repository import payload_rechazo, registrar_rechazo
from seace_monitor.logging import PASO_OCR, PASO_PDF, registrar_run

cargar_env()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Plantillas reales capturadas (descubrir_endpoint_pdf.py). Override por env.
LISTAR_URL = os.environ.get(
    "LISTAR_URL",
    DEFAULT_LISTAR_URL,
).strip()
DESCARGAR_URL = os.environ.get(
    "DESCARGAR_URL",
    DEFAULT_DESCARGAR_URL,
).strip()

MIN_CHARS_PAGINA = 80
OCR_MAX_PAGINAS = 40
OCR_DPI = 150
DELAY_S = 0.35
OCR_RPM = 0.0
_OCR_NEXT = 0.0
TEMP_PREFIX = "seace-tdr-"
PREVIEW_CHARS = 1_500
FLASH_OCR_MAX_DIA = 6_000
# Gemini 3 Flash Preview (aprox. 3.7 Flash): tarifa paga de referencia.
# Modelo y contadores OCR vienen del adaptador; precios de seace_monitor.gemini.


def parse_ids(raw: str) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for part in re.split(r"[,\s]+", raw.strip()):
        if part:
            out.append(int(part))
    return out


def respetar_rpm() -> None:
    global _OCR_NEXT
    if OCR_RPM <= 0:
        return
    now = time.time()
    if now < _OCR_NEXT:
        time.sleep(_OCR_NEXT - now)
    _OCR_NEXT = time.time() + (60.0 / OCR_RPM)


def listar_archivos(http: SeaceHttp, cid: int) -> tuple[str, list]:
    """Conserva la firma histórica usando la plantilla configurada al iniciar."""
    return listar_archivos_seace(http, cid, LISTAR_URL)


def resolver_cfg(endpoint: str):
    """Costura inyectable: config dinámica ia_* o None → camino por env."""
    return cfg_resuelta(endpoint)


def _cfg_ocr():
    """Config dinámica utilizable para OCR (con credencial) o None."""
    return cfg_con_credencial("ocr", resolver=resolver_cfg)


def ocr_pagina_gemini(img_bytes: bytes, mime: str = "image/jpeg") -> str:
    cfg = _cfg_ocr()
    if cfg is None and not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY ausente; no se puede hacer OCR")
    respetar_rpm()
    if cfg is None:
        fijar_ocr_activo(None)
        # Camino histórico por env: mismo punto de inyección documentado.
        return limpiar_texto(
            solicitar_ocr_gemini(httpx, img_bytes, mime, GEMINI_API_KEY)
        )
    return ocr_con_contingencia(
        httpx, cfg, img_bytes, mime, gemini_key=GEMINI_API_KEY)


def extraer_paginas(path: Path, *, permitir_ocr: bool = True) -> dict:
    """Wrapper compatible sobre el extractor independiente del CLI."""
    return extraer_paginas_documento(
        path,
        permitir_ocr=permitir_ocr,
        ocr_page=ocr_pagina_gemini,
        min_chars_pagina=MIN_CHARS_PAGINA,
        ocr_max_paginas=OCR_MAX_PAGINAS,
        ocr_dpi=OCR_DPI,
    )


def procesar_contrato(
    http: SeaceHttp,
    contrato: dict,
    *,
    permitir_ocr: bool = True,
    supa=None,
) -> dict:
    """Wrapper compatible sobre el servicio documental."""
    return procesar_documento(
        http,
        contrato,
        listar_url=LISTAR_URL,
        descargar_url=DESCARGAR_URL,
        permitir_ocr=permitir_ocr,
        ocr_page=ocr_pagina_gemini,
        supa=supa,
        min_chars_pagina=MIN_CHARS_PAGINA,
        ocr_max_paginas=OCR_MAX_PAGINAS,
        ocr_dpi=OCR_DPI,
    )


def _update_contrato(supa, cid: int, payload: dict) -> None:
    actualizar_contrato_documental(supa, cid, payload)


def rechunk_embed_pdf(supa, cid: int) -> None:
    """Fachada histórica para consumidores externos del entrypoint."""
    from generar_embeddings import embed_columna_reserva
    extras = extras_embeddings(cfg_resuelta("embeddings"))
    api_key = extras.pop("api_key", GEMINI_API_KEY)
    _rechunk_embed_pdf(supa, cid, api_key=api_key, **extras)
    embed_columna_reserva(supa, cid, extras.get("columna", "embedding_v2"))


def ocr_contrato_selectivo(
    http: SeaceHttp,
    supa,
    contrato: dict,
    cuota: dict,
    max_dia: int,
    *,
    t0: float,
    max_segundos: int,
) -> dict:
    """Wrapper compatible sobre el servicio OCR selectivo."""
    return procesar_paginas_pendientes(
        http,
        supa,
        contrato,
        cuota,
        max_dia,
        t0=t0,
        max_segundos=max_segundos,
        listar_url=LISTAR_URL,
        descargar_url=DESCARGAR_URL,
        ocr_page=ocr_pagina_gemini,
        append_ocr=anexar_ocr_a_tdr,
        save_progress=guardar_ocr_progreso,
        register_success=registrar_ocr_ok,
        persist_storage=persistir_storage_si_hay,
        ocr_dpi=OCR_DPI,
        temp_prefix=TEMP_PREFIX,
    )


def imprimir_resultado(i: int, total: int, contrato: dict, row: dict,
                       estado: str = "OK") -> None:
    cid = row["id"]
    desc = (contrato.get("descripcion_contrato") or "")[:50]
    digest = row.get("pdf_hash") or ""
    print(
        f"  [{i}/{total}] id={cid} {estado} {desc}\n"
        f"      listados={row.get('n_archivos')}  "
        f"elegido={row.get('pdf_nombre')!r}  "
        f"mime={row.get('pdf_mime')}  "
        f"idTipoArchivo={row.get('id_tipo_archivo')}  "
        f"idContratoArchivo={row.get('pdf_archivo_id')}",
        flush=True,
    )
    print(
        f"      paginas={row.get('n_paginas')} bytes={row.get('bytes')} "
        f"hash={(digest[:12] + '...') if digest else '-'} "
        f"pymupdf_chars={row.get('chars_pymupdf')} "
        f"final_chars={row.get('chars_final')} "
        f"ocr_paginas={row.get('ocr_paginas') or '-'} "
        f"tipo={row.get('tdr_tipo_extraccion') or '-'} "
        f"pdf_es_imagen={row.get('pdf_es_imagen')} "
        f"temp={('AUN EXISTE' if Path(row.get('temp_path') or '').exists() else 'borrado')}",
        flush=True,
    )
    for p in row["por_pagina"]:
        marca = "OCR" if p.get("ocr") else "pymupdf"
        extra = f" omitido={p['omitido']}" if p.get("omitido") else ""
        print(
            f"      p{p['pagina']}: {marca}  "
            f"pymupdf={p['chars_pymupdf']} final={p['chars_final']}{extra}",
            flush=True,
        )
    preview = (row["tdr_texto"] or "")[:PREVIEW_CHARS].replace("\n", " | ")
    mas = "..." if len(row["tdr_texto"] or "") > PREVIEW_CHARS else ""
    print(
        f"      texto ({len(row['tdr_texto'] or '')} chars): {preview}{mas}",
        flush=True,
    )


def imprimir_sin_pdf(i: int, total: int, contrato: dict, archivos: list) -> None:
    cid = int(contrato["id"])
    desc = (contrato.get("descripcion_contrato") or "")[:50]
    print(
        f"  [{i}/{total}] id={cid} SIN_PDF {desc}  listados={len(archivos)}",
        flush=True,
    )
    for a in resumen_archivos(archivos):
        print(
            f"      archivo={a.get('nombre')!r} mime={a.get('descripcionMime')} "
            f"tipo={a.get('idTipoArchivo')} id={a.get('idContratoArchivo')}",
            flush=True,
        )
    if not archivos:
        print("      (listado vacio)", flush=True)


def imprimir_linea(i: int, total: int, cid: int, estado: str, extra: str = "") -> None:
    print(f"  [{i}/{total}] id={cid} {estado}{('  ' + extra) if extra else ''}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Tope de contratos (0 = todos los pendientes del modo)",
    )
    ap.add_argument(
        "--ids",
        default="",
        help="Ids fijos separados por coma (ignora pdf_descargado=false)",
    )
    ap.add_argument(
        "--rpm",
        type=float,
        default=0,
        help="Tope de llamadas OCR/minuto (0 = 6 si --solo-ocr, si no sin tope extra)",
    )
    modo = ap.add_mutually_exclusive_group()
    modo.add_argument(
        "--solo-nativo",
        action="store_true",
        help="PyMuPDF por página: guarda texto nativo; marca páginas imagen (sin Flash)",
    )
    modo.add_argument(
        "--solo-ocr",
        action="store_true",
        help="OCR solo páginas imagen pendientes (append a tdr_texto, tope 6K Flash/día)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Procesa y muestra; no escribe contratos ni rechazos",
    )
    ap.add_argument(
        "--reporte",
        action="store_true",
        help="Solo imprime conteo de tipos (BD y/o jsonl); no descarga",
    )
    ap.add_argument(
        "--sync-meta",
        action="store_true",
        help="Aplica data/tdr_extraccion.jsonl a las columnas nuevas (tras el SQL)",
    )
    ap.add_argument(
        "--max-ocr-dia",
        type=int,
        default=FLASH_OCR_MAX_DIA,
        help="Tope Flash OCR/día (reserva chat: nunca más de 6000)",
    )
    ap.add_argument(
        "--solo-ti",
        action="store_true",
        help="OCR solo categoria_it o relevancia_ia (ambos NULL = no gasta Flash)",
    )
    ap.add_argument(
        "--incluir-por-abrir",
        action="store_true",
        help="OCR también contratos con fecha_ini futura (default: solo postulables)",
    )
    ap.add_argument(
        "--max-segundos",
        type=int,
        default=OCR_MAX_SEGUNDOS_DEFAULT,
        help="Tope de reloj OCR (0 = sin tope; cupo Flash sigue). Default 7200 = 2h",
    )
    ap.add_argument(
        "--reintentar-sin-pdf",
        action="store_true",
        help="Incluye en la cola los req_url='sin_pdf' con pdf_descargado=true "
        "(los descartados antes por el mime de SEACE)",
    )
    ap.add_argument("--headed", action="store_true",
                    help="Playwright headed (solo si hay fallback 401/403)")
    args = ap.parse_args()

    global OCR_RPM
    if args.solo_ocr and (not args.rpm or args.rpm <= 0):
        OCR_RPM = 6.0
    else:
        OCR_RPM = args.rpm if args.rpm and args.rpm > 0 else 0.0
    if args.solo_nativo:
        modo_sel = "nativo"
        permitir_ocr = False
    elif args.solo_ocr:
        modo_sel = "ocr"
        permitir_ocr = True
    else:
        modo_sel = "todos"
        permitir_ocr = True
    if args.reintentar_sin_pdf and modo_sel in ("todos", "nativo"):
        modo_sel = "sin_pdf"
    compacto = args.solo_nativo or args.solo_ocr or args.limit == 0

    supa = crear_cliente()
    if args.sync_meta:
        sync_meta_jsonl(supa)
    if args.reporte or args.sync_meta:
        counts = conteo_pdf(supa)
        print("=" * 60, flush=True)
        print("PASO 1-bis — reporte", flush=True)
        print(
            f"  vigentes={counts['vigentes']}  "
            f"pendientes={counts['pendientes']}  "
            f"ya_ok={counts['ya_descargados']}  "
            f"marcados_ocr={counts['pendiente_ocr']}",
            flush=True,
        )
        jl = reporte_jsonl()
        print("  --- jsonl (re-extracción) ---", flush=True)
        for k, v in jl.items():
            print(f"    {k}={v}", flush=True)
        if columnas_extraccion_ok(supa):
            tipos = reporte_extraccion(supa)
            print("  --- vigentes BD ---", flush=True)
            for k, v in tipos.items():
                print(f"    {k}={v}", flush=True)
            gb = group_by_tipo(supa, vigentes=True)
            print("  --- GROUP BY tdr_tipo_extraccion (vigentes) ---", flush=True)
            for k, v in sorted(gb.items(), key=lambda kv: (-kv[1], kv[0])):
                print(f"    {k}={v}", flush=True)
            gb_all = group_by_tipo(supa, vigentes=False)
            print("  --- GROUP BY tdr_tipo_extraccion (todos) ---", flush=True)
            for k, v in sorted(gb_all.items(), key=lambda kv: (-kv[1], kv[0])):
                print(f"    {k}={v}", flush=True)
        else:
            print("  (columnas nuevas aún no aplicadas)", flush=True)
        print("=" * 60, flush=True)
        if args.reporte:
            return
        if args.sync_meta and not args.solo_nativo and not args.solo_ocr and not args.ids:
            return

    if args.solo_ocr:
        ids = parse_ids(args.ids)
        max_dia = min(
            args.max_ocr_dia if args.max_ocr_dia > 0 else FLASH_OCR_MAX_DIA,
            FLASH_OCR_MAX_DIA,
        )
        run_ocr_selectivo(
            supa,
            ocr_contrato=ocr_contrato_selectivo,
            rechunk=rechunk_embed_pdf,
            imprimir=imprimir_linea,
            limit=args.limit,
            ids=ids,
            headed=args.headed,
            max_dia=max_dia,
            dry_run=args.dry_run,
            solo_ti=args.solo_ti,
            max_segundos=args.max_segundos,
            incluir_por_abrir=args.incluir_por_abrir,
            rpm=OCR_RPM,
            delay_s=DELAY_S,
        )
        return

    ids = parse_ids(args.ids)
    ejecutar_descarga_batch(
        supa,
        ids=ids,
        limit=args.limit,
        modo=modo_sel,
        permitir_ocr=permitir_ocr,
        dry_run=args.dry_run,
        headed=args.headed,
        compacto=compacto,
        rpm=OCR_RPM,
        delay_s=DELAY_S,
        temp_prefix=TEMP_PREFIX,
        listar_url=LISTAR_URL,
        descargar_url=DESCARGAR_URL,
        gemini_habilitado=(
            bool(GEMINI_API_KEY)
            or _cfg_ocr() is not None
            or embeddings_configurados(resolver_cfg)
        ),
        procesar=procesar_contrato,
        imprimir_linea=imprimir_linea,
        imprimir_resultado=imprimir_resultado,
        imprimir_sin_pdf=imprimir_sin_pdf,
    )


if __name__ == "__main__":
    main()
