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


def ocr_pagina_gemini(img_bytes: bytes, mime: str = "image/jpeg") -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY ausente; no se puede hacer OCR")
    respetar_rpm()
    text = solicitar_ocr_gemini(httpx, img_bytes, mime, GEMINI_API_KEY)
    return limpiar_texto(text)


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
    _rechunk_embed_pdf(supa, cid, api_key=GEMINI_API_KEY)


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

    if not args.dry_run and not columnas_extraccion_ok(supa):
        print(
            "  [warn] columnas de extracción ausentes; "
            "se guarda tdr_texto + jsonl. Corre tdr_extraccion_meta.sql "
            "y --sync-meta después.",
            flush=True,
        )
    counts = conteo_pdf(supa)
    ids = parse_ids(args.ids)
    if ids:
        filas = contratos_por_ids(supa, ids)
    else:
        filas = pendientes_pdf(supa, args.limit, modo=modo_sel)

    print("=" * 60, flush=True)
    print("Fase 3 — PDF/TDR (listar + descargar, httpx)", flush=True)
    print(
        f"  modo={modo_sel}  dry-run={args.dry_run}  limit={args.limit}  "
        f"ids={ids or '-'}  cola={len(filas)}  rpm={OCR_RPM or '-'}",
        flush=True,
    )
    print(
        f"  vigentes={counts['vigentes']}  "
        f"pendientes={counts['pendientes']}  "
        f"ya_ok={counts['ya_descargados']}  "
        f"marcados_ocr={counts['pendiente_ocr']}",
        flush=True,
    )
    print(f"  LISTAR_URL={LISTAR_URL}", flush=True)
    print(f"  DESCARGAR_URL={DESCARGAR_URL}", flush=True)
    print(f"  GEMINI_API_KEY set={bool(GEMINI_API_KEY)}  (OCR fallback)", flush=True)
    print("=" * 60, flush=True)

    if not filas:
        print("Nada que hacer (cola vacia para este modo).", flush=True)
        jl = reporte_jsonl()
        print("\n--- PASO 1-bis jsonl ---", flush=True)
        for k, v in jl.items():
            print(f"  {k}={v}", flush=True)
        extra = dict(jl)
        if columnas_extraccion_ok(supa):
            tipos = reporte_extraccion(supa)
            print("\n--- PASO 1-bis vigentes BD ---", flush=True)
            for k, v in tipos.items():
                print(f"  {k}={v}", flush=True)
            extra.update(tipos)
        escribir_resumen(supa, {**counts, **extra, "ok": 0, "modo": modo_sel,
                          "limit": args.limit, "dry_run": args.dry_run,
                          "elapsed_s": 0})
        return

    ok = 0
    n_puro = 0
    n_mixto = 0
    n_imagen = 0
    ocr_paginas_total = 0
    skip_ocr = 0
    ocr_paginas_estimadas = 0
    sin_pdf = 0
    no_pdf = 0
    reintentados = 0
    err = 0
    t0 = time.time()
    leftovers_antes = {
        p.name for p in Path(tempfile.gettempdir()).glob(f"{TEMP_PREFIX}*")
    }

    http = SeaceHttp(headed=args.headed)
    try:
        for i, c in enumerate(filas, 1):
            cid = int(c["id"])
            desc = (c.get("descripcion_contrato") or "")[:50]
            if (c.get("req_url") or "") == "sin_pdf":
                reintentados += 1
            try:
                row = procesar_contrato(
                    http, c, permitir_ocr=permitir_ocr, supa=None if args.dry_run else supa
                )
                tipo = row.get("tdr_tipo_extraccion") or clasificar_tipo(
                    int(row.get("n_paginas") or 0),
                    list(row.get("ocr_paginas") or []),
                )
                n_ocr = int(row.get("n_paginas_ocr") or len(row.get("ocr_paginas") or []))
                ocr_paginas_total += n_ocr
                if tipo == "nativo_puro":
                    n_puro += 1
                    etiqueta = "NATIVO_PURO"
                elif tipo == "mixto":
                    n_mixto += 1
                    etiqueta = "MIXTO"
                else:
                    n_imagen += 1
                    etiqueta = "IMAGEN_TOTAL"
                if compacto:
                    imprimir_linea(
                        i, len(filas), cid, etiqueta,
                        f"pags={row.get('n_paginas')} "
                        f"nat={row.get('n_paginas_nativas')} "
                        f"ocr={n_ocr} chars={row.get('chars_final')} "
                        f"acum_ocr={ocr_paginas_total}",
                    )
                else:
                    imprimir_resultado(i, len(filas), c, row)
                if not args.dry_run:
                    guardar_ok(supa, row)
                ok += 1
            except NecesitaOcr as e:
                skip_ocr += 1
                n_est = len(e.meta.get("ocr_paginas") or [])
                ocr_paginas_estimadas += n_est
                imprimir_linea(
                    i, len(filas), cid, "SKIP_OCR",
                    f"pags={e.meta.get('n_paginas')} ocr_pags={n_est} "
                    f"estim_acum={ocr_paginas_estimadas}",
                )
                if not args.dry_run:
                    guardar_pendiente_ocr(supa, cid, e.meta)
            except SinPdf as e:
                sin_pdf += 1
                if compacto:
                    imprimir_linea(i, len(filas), cid, "SIN_PDF",
                                   f"archivos={len(e.archivos)}")
                else:
                    imprimir_sin_pdf(i, len(filas), c, e.archivos)
                if not args.dry_run:
                    guardar_sin_pdf(supa, cid)
                    registrar_rechazo(
                        supa,
                        payload_rechazo(
                            c,
                            MOTIVO_SIN_PDF,
                            {"archivos": resumen_archivos(e.archivos)},
                        ),
                        MOTIVO_SIN_PDF,
                        origen="pdf",
                    )
            except NoEsPdf as e:
                # Habia anexo candidato a PDF y el binario no lo era. No
                # reintentar (guardar_sin_pdf marca req_url='sin_pdf'), pero
                # queda registrado con motivo distinto para poder contarlo.
                no_pdf += 1
                imprimir_linea(i, len(filas), cid, "NO_PDF", desc)
                if not args.dry_run:
                    guardar_sin_pdf(supa, cid)
                    registrar_rechazo(
                        supa,
                        payload_rechazo(c, str(e)[:500]),
                        MOTIVO_NO_PDF,
                        origen="pdf",
                    )
            except PdfExtractError as e:
                err += 1
                imprimir_linea(i, len(filas), cid, f"FAIL ({e})", desc)
                if not args.dry_run:
                    persistir_storage_si_hay(supa, cid, e.meta or {})
                    registrar_rechazo(
                        supa,
                        payload_rechazo(c, str(e)[:500], {
                            "archivos": e.meta.get("archivos"),
                            "pdf_nombre": e.meta.get("pdf_nombre"),
                        }),
                        str(e),
                        origen="pdf",
                    )
            except Exception as e:
                err += 1
                imprimir_linea(i, len(filas), cid, f"FAIL {e}", desc)
                if not args.dry_run:
                    registrar_rechazo(
                        supa,
                        payload_rechazo(c, str(e)[:500]),
                        str(e),
                        origen="pdf",
                    )
            if i % 20 == 0:
                elapsed = time.time() - t0
                print(
                    f"  -- progreso {i}/{len(filas)}  "
                    f"puro={n_puro} mixto={n_mixto} imagen={n_imagen} "
                    f"pags_ocr={ocr_paginas_total} sin_pdf={sin_pdf} "
                    f"no_pdf={no_pdf} err={err} t={elapsed:.0f}s",
                    flush=True,
                )
            time.sleep(DELAY_S)
    finally:
        http.close()

    leftovers = [
        p.name
        for p in Path(tempfile.gettempdir()).glob(f"{TEMP_PREFIX}*")
        if p.name not in leftovers_antes
    ]
    elapsed = time.time() - t0
    counts_fin = conteo_pdf(supa)
    tipos = reporte_extraccion(supa) if columnas_extraccion_ok(supa) else {}
    print(f"\n{'='*60}", flush=True)
    print(
        f"Listo en {elapsed:.0f}s  ok={ok} "
        f"nativo_puro={n_puro} mixto={n_mixto} imagen_total={n_imagen} "
        f"pags_ocr_cola={ocr_paginas_total} "
        f"sin_pdf={sin_pdf} no_pdf={no_pdf} reintentados={reintentados} "
        f"err={err} dry-run={args.dry_run}",
        flush=True,
    )
    print(
        f"Vigentes={counts_fin['vigentes']}  "
        f"pendientes={counts_fin['pendientes']}  "
        f"marcados_ocr={counts_fin['pendiente_ocr']}  "
        f"ya_ok={counts_fin['ya_descargados']}",
        flush=True,
    )
    if tipos:
        print("\n--- PASO 1-bis vigentes (BD) ---", flush=True)
        print(f"  nativo_puro={tipos['nativo_puro']}", flush=True)
        print(f"  mixto={tipos['mixto']}", flush=True)
        print(f"  imagen_total={tipos['imagen_total']}", flush=True)
        print(f"  paginas_ocr_reales={tipos['paginas_ocr_reales']}", flush=True)
        print(f"  paginas_nativas={tipos['paginas_nativas']}", flush=True)
        print(f"  paginas_totales={tipos['paginas_totales']}", flush=True)
        print(f"  sin_pdf={tipos['sin_pdf']}", flush=True)
        print(f"  pendiente_ocr_viejo={tipos['pendiente_ocr_viejo']}", flush=True)
        print(f"  sin_tipo={tipos['sin_tipo']}", flush=True)
    print(f"OCR_PAGINAS_REALES_COLA={ocr_paginas_total}", flush=True)
    print(
        f"Temps {TEMP_PREFIX}* residuales de esta corrida: "
        f"{leftovers if leftovers else 'ninguno (borrados)'}",
        flush=True,
    )
    print("=" * 60, flush=True)
    escribir_resumen(supa, {
        "modo": modo_sel,
        "ok": ok,
        "nativo_puro_cola": n_puro,
        "mixto_cola": n_mixto,
        "imagen_total_cola": n_imagen,
        "ocr_paginas_cola": ocr_paginas_total,
        "skip_ocr": skip_ocr,
        "ocr_paginas_estimadas": ocr_paginas_estimadas,
        "sin_pdf_cola": sin_pdf,
        "err": err,
        "limit": args.limit,
        "dry_run": args.dry_run,
        "elapsed_s": int(elapsed),
        "temps_residuales": ",".join(leftovers),
        **{f"fin_{k}": v for k, v in counts_fin.items()},
        **{f"bd_{k}": v for k, v in tipos.items()},
    })


if __name__ == "__main__":
    main()
