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
    chars_utiles,
    clasificar_tipo,
    extraer_paginas as extraer_paginas_documento,
    limpiar_texto,
    pdf_sha256,
)
from seace_monitor.documents.repository import (
    contratos_por_ids,
    pendientes_pdf,
    update_contrato as actualizar_contrato_documental,
)
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
from seace_monitor.ocr.gemini_provider import (
    GEMINI_FLASH,
    LAST_OCR_USAGE,
    OCR_USAGE_ACUM,
    CupoFlash,
    solicitar_ocr_gemini,
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
from seace_monitor.ocr.service import procesar_paginas_pendientes

import argparse
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from seace_monitor.supabase_client import crear_cliente

from seace_monitor.ingestion.repository import registrar_rechazo
from pipeline_log import PASO_OCR, PASO_PDF, registrar_run

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

PAGE_DB = 1_000
MIN_CHARS_PAGINA = 80
COLS_EXTRACCION = (
    "tdr_tipo_extraccion",
    "paginas_ocr_pendientes",
    "paginas_ocr_hechas",
    "tdr_n_paginas",
    "tdr_n_paginas_nativas",
    "tdr_n_paginas_ocr",
)
OCR_MAX_PAGINAS = 40
OCR_DPI = 150
DELAY_S = 0.35
OCR_RPM = 0.0
_OCR_NEXT = 0.0
TEMP_PREFIX = "seace-tdr-"
PREVIEW_CHARS = 1_500
REQ_PENDIENTE_OCR = "pendiente_ocr"
META_LOG = Path(__file__).parent / "data" / "tdr_extraccion.jsonl"
CUOTA_OCR_PATH = Path(__file__).parent / "data" / "flash_ocr_cuota.json"
CUOTA_OCR_TABLA = "pipeline_cuota_ocr"
OCR_LOG = Path(__file__).parent / "data" / "ultima_ocr.txt"
OCR_MAX_SEGUNDOS_DEFAULT = 7_200
FLASH_OCR_MAX_DIA = 6_000
USD_PEN = 3.75
GASTO_STOP_PEN = 2.0
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


def registrar_meta_local(row: dict) -> None:
    """Sidecar idempotente (última línea por id gana) por si aún no hay DDL."""
    tipo = row.get("tdr_tipo_extraccion") or clasificar_tipo(
        int(row.get("n_paginas") or 0),
        list(row.get("ocr_paginas") or []),
    )
    rec = {
        "id": int(row["id"]),
        "tdr_tipo_extraccion": tipo,
        "paginas_ocr_pendientes": list(row.get("ocr_paginas") or []),
        "paginas_ocr_hechas": list(row.get("ocr_hechas") or []),
        "tdr_n_paginas": int(row.get("n_paginas") or 0),
        "tdr_n_paginas_nativas": int(row.get("n_paginas_nativas") or 0),
        "tdr_n_paginas_ocr": int(row.get("n_paginas_ocr") or 0),
        "pdf_es_imagen": tipo != "nativo_puro",
        "chars_final": int(row.get("chars_final") or 0),
    }
    META_LOG.parent.mkdir(parents=True, exist_ok=True)
    with META_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def reporte_jsonl() -> dict:
    if not META_LOG.exists():
        return {
            "nativo_puro": 0, "mixto": 0, "imagen_total": 0,
            "paginas_ocr_reales": 0, "paginas_nativas": 0, "paginas_totales": 0,
            "contratos": 0,
        }
    by_id: dict[int, dict] = {}
    for line in META_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        by_id[int(rec["id"])] = rec
    nativo = mixto = imagen = 0
    pags_ocr = pags_nat = pags_tot = 0
    for rec in by_id.values():
        tipo = rec.get("tdr_tipo_extraccion")
        if tipo == "nativo_puro":
            nativo += 1
        elif tipo == "mixto":
            mixto += 1
        elif tipo == "imagen_total":
            imagen += 1
        pags_ocr += int(rec.get("tdr_n_paginas_ocr") or 0)
        pags_nat += int(rec.get("tdr_n_paginas_nativas") or 0)
        pags_tot += int(rec.get("tdr_n_paginas") or 0)
    return {
        "nativo_puro": nativo,
        "mixto": mixto,
        "imagen_total": imagen,
        "paginas_ocr_reales": pags_ocr,
        "paginas_nativas": pags_nat,
        "paginas_totales": pags_tot,
        "contratos": len(by_id),
    }


def payload_extraccion(rec: dict) -> dict:
    tipo = rec.get("tdr_tipo_extraccion")
    pend = rec.get("paginas_ocr_pendientes")
    if pend is None:
        pend = rec.get("ocr_paginas") or []
    hechas = rec.get("paginas_ocr_hechas")
    if hechas is None:
        hechas = rec.get("ocr_hechas") or []
    pdf_img = rec.get("pdf_es_imagen")
    if pdf_img is None:
        pdf_img = tipo != "nativo_puro" if tipo else None
    return {
        "pdf_es_imagen": pdf_img,
        "tdr_tipo_extraccion": tipo,
        "paginas_ocr_pendientes": list(pend or []),
        "paginas_ocr_hechas": list(hechas or []),
        "tdr_n_paginas": rec.get("tdr_n_paginas") or rec.get("n_paginas"),
        "tdr_n_paginas_nativas": rec.get("tdr_n_paginas_nativas")
        or rec.get("n_paginas_nativas"),
        "tdr_n_paginas_ocr": rec.get("tdr_n_paginas_ocr")
        or rec.get("n_paginas_ocr"),
    }


def _update_extraccion(supa, cid: int, payload: dict) -> None:
    """Escribe columnas de extracción. No traga errores (el slim de
    `_update_contrato` dejaba mixto/imagen en NULL si PostgREST aún no
    cacheaba el DDL)."""
    try:
        supa.table("contratos").update(payload).eq("id", cid).execute()
    except Exception as e:
        raise RuntimeError(f"sync id={cid} falló: {e}") from e


def group_by_tipo(supa, *, vigentes: bool = True) -> dict[str, int]:
    """Equivalente a SELECT tdr_tipo_extraccion, COUNT(*) GROUP BY 1."""
    out: dict[str, int] = {}
    offset = 0
    while True:
        q = supa.table("contratos").select("tdr_tipo_extraccion,req_url,pdf_es_imagen")
        if vigentes:
            q = q.eq("estado", "Vigente")
        # PostgREST: .range() sin .order() no garantiza orden entre paginas;
        # la pagina 2 puede repetir filas de la 1 y omitir otras.
        res = q.order("id").range(offset, offset + PAGE_DB - 1).execute()
        batch = res.data or []
        for r in batch:
            _aplanar_cl(r)
            tipo = r.get("tdr_tipo_extraccion")
            key = tipo if tipo else "NULL"
            out[key] = out.get(key, 0) + 1
        if len(batch) < PAGE_DB:
            break
        offset += PAGE_DB
    return out


def sync_meta_jsonl(supa) -> int:
    if not columnas_extraccion_ok(supa):
        raise SystemExit(
            "ERROR: faltan columnas. Ejecuta tdr_extraccion_meta.sql "
            "(y NOTIFY pgrst, 'reload schema') y reintenta --sync-meta"
        )
    if not META_LOG.exists():
        print("  [sync] no hay data/tdr_extraccion.jsonl", flush=True)
        return 0
    by_id: dict[int, dict] = {}
    for line in META_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rec = json.loads(line)
            by_id[int(rec["id"])] = rec
    print(
        f"  [sync] jsonl ids={len(by_id)}  "
        + " ".join(
            f"{k}={sum(1 for r in by_id.values() if r.get('tdr_tipo_extraccion')==k)}"
            for k in ("nativo_puro", "mixto", "imagen_total")
        ),
        flush=True,
    )
    probe_id, probe_rec = next(iter(by_id.items()))
    probe_payload = payload_extraccion(probe_rec)
    _update_extraccion(supa, probe_id, probe_payload)
    check = (
        supa.table("contratos")
        .select("tdr_tipo_extraccion")
        .eq("id", probe_id)
        .limit(1)
        .execute()
    )
    got = (check.data or [{}])[0].get("tdr_tipo_extraccion")
    want = probe_payload.get("tdr_tipo_extraccion")
    if got != want:
        raise SystemExit(
            f"ERROR: PostgREST no persistió tdr_tipo_extraccion "
            f"(id={probe_id} escribió={want!r} leyó={got!r}). "
            f"En SQL Editor: NOTIFY pgrst, 'reload schema'; y reintenta."
        )
    print(f"  [sync] probe id={probe_id} tipo={got} OK", flush=True)

    n = 0
    err = 0
    for cid, rec in by_id.items():
        if cid == probe_id:
            n += 1
            continue
        try:
            _update_extraccion(supa, cid, payload_extraccion(rec))
            n += 1
        except Exception as e:
            err += 1
            print(f"  [sync] FAIL id={cid}: {e}", flush=True)
            if err >= 5:
                raise SystemExit("ERROR: demasiados fallos de sync; aborto")
        if n % 200 == 0:
            print(f"  [sync] {n}/{len(by_id)}", flush=True)
    print(f"  [sync] jsonl escritos={n} err={err}", flush=True)

    # Gemelos con texto nativo que quedaron pdf_es_imagen=true sin sidecar.
    n_nat = 0
    offset = 0
    while True:
        res = (
            supa.table("contratos")
            .select("id,tdr_texto,pdf_es_imagen,tdr_tipo_extraccion,req_url")
            .eq("estado", "Vigente")
            .eq("pdf_es_imagen", True)
            .is_("tdr_tipo_extraccion", "null")
            # PostgREST: .range() sin .order() no garantiza orden entre paginas;
            # la pagina 2 puede repetir filas de la 1 y omitir otras.
            .order("id")
            .range(offset, offset + PAGE_DB - 1)
            .execute()
        )
        batch = res.data or []
        for r in batch:
            _aplanar_cl(r)
            cid = int(r["id"])
            if cid in by_id:
                continue
            tdr = r.get("tdr_texto") or ""
            if (r.get("req_url") or "") == "sin_pdf":
                continue
            if "(ocr)" in tdr or chars_utiles(tdr) < MIN_CHARS_PAGINA:
                print(
                    f"  [sync] huerfano id={cid} no clasificado "
                    f"(chars={chars_utiles(tdr)})",
                    flush=True,
                )
                continue
            _update_extraccion(supa, cid, {
                "tdr_tipo_extraccion": "nativo_puro",
                "pdf_es_imagen": False,
                "paginas_ocr_pendientes": [],
                "paginas_ocr_hechas": [],
                "tdr_n_paginas_ocr": 0,
            })
            n_nat += 1
            print(f"  [sync] huerfano id={cid} → nativo_puro", flush=True)
        if len(batch) < PAGE_DB:
            break
        offset += PAGE_DB
    if n_nat:
        print(f"  [sync] huerfanos nativo_puro={n_nat}", flush=True)
    return n + n_nat


def _as_int_list(raw) -> list[int]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = json.loads(raw)
    out: list[int] = []
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def cargar_cuota_ocr(supa=None) -> dict:
    """Cuota Flash OCR del día Lima. Fuente de verdad: BD (pipeline_cuota_ocr);
    fallback al archivo local si no hay cliente o la tabla no responde."""
    hoy = fecha_lima()
    if supa is not None:
        try:
            res = (
                supa.table(CUOTA_OCR_TABLA)
                .select("*")
                .eq("fecha_lima", hoy)
                .maybe_single()
                .execute()
            )
            # supabase-py 2.x: maybe_single() devuelve None (no .data) sin fila.
            row = getattr(res, "data", None)
            if row:
                return {
                    "fecha": hoy,
                    "requests": int(row.get("requests") or 0),
                    "prompt_tokens": int(row.get("prompt_tokens") or 0),
                    "out_tokens": int(row.get("out_tokens") or 0),
                    "usd_est": float(row.get("usd_est") or 0.0),
                }
        except Exception as e:
            print(f"  [warn] cargar_cuota_ocr BD: {e}", flush=True)
    if CUOTA_OCR_PATH.exists():
        try:
            d = json.loads(CUOTA_OCR_PATH.read_text(encoding="utf-8"))
            if d.get("fecha") == hoy:
                d.setdefault("requests", 0)
                d.setdefault("prompt_tokens", 0)
                d.setdefault("out_tokens", 0)
                d.setdefault("usd_est", 0.0)
                return d
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "fecha": hoy,
        "requests": 0,
        "prompt_tokens": 0,
        "out_tokens": 0,
        "usd_est": 0.0,
    }


def guardar_cuota_ocr(supa, d: dict) -> None:
    if supa is not None:
        try:
            supa.table(CUOTA_OCR_TABLA).upsert(
                {
                    "fecha_lima": d["fecha"],
                    "requests": int(d.get("requests") or 0),
                    "prompt_tokens": int(d.get("prompt_tokens") or 0),
                    "out_tokens": int(d.get("out_tokens") or 0),
                    "usd_est": float(d.get("usd_est") or 0.0),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="fecha_lima",
            ).execute()
        except Exception as e:
            print(f"  [warn] guardar_cuota_ocr BD: {e}", flush=True)
    # Respaldo local (auditoría en git vía el diario). No es la fuente de verdad.
    CUOTA_OCR_PATH.parent.mkdir(parents=True, exist_ok=True)
    CUOTA_OCR_PATH.write_text(
        json.dumps(d, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def registrar_ocr_ok(supa, cuota: dict, max_dia: int) -> None:
    prompt = int(LAST_OCR_USAGE.get("prompt") or 0)
    out = int(LAST_OCR_USAGE.get("candidates") or 0)
    page_usd = usd_de_tokens(prompt, out)
    cuota["requests"] = int(cuota.get("requests") or 0) + 1
    cuota["prompt_tokens"] = int(cuota.get("prompt_tokens") or 0) + prompt
    cuota["out_tokens"] = int(cuota.get("out_tokens") or 0) + out
    cuota["usd_est"] = float(cuota.get("usd_est") or 0) + page_usd
    guardar_cuota_ocr(supa, cuota)
    if supa is not None:
        try:
            supa.table("uso_ia").insert({
                "componente": "ocr",
                "modelo": GEMINI_FLASH,
                "tokens_prompt": prompt,
                "tokens_completion": out,
                "tokens_total": prompt + out,
                "costo_usd": page_usd,
                "cache_hit": False,
            }).execute()
        except Exception as e:
            print(f"  [warn] log_uso_ia OCR: {e}", flush=True)
    if int(cuota["requests"]) >= max_dia:
        raise CupoFlash(
            f"tope diario {max_dia} Flash (usadas={cuota['requests']})",
            motivo="cupo",
        )


def anexar_ocr_a_tdr(tdr: str, pagina: int, texto: str) -> str:
    marca = f"--- pagina {pagina} (ocr) ---"
    bloque = f"{marca}\n{(texto or '').strip()}".strip()
    base = (tdr or "").strip()
    if marca in base:
        return base
    if not base:
        return bloque
    return base + "\n\n" + bloque


def meta_local_por_id() -> dict[int, dict]:
    by_id: dict[int, dict] = {}
    if not META_LOG.exists():
        return by_id
    for line in META_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        by_id[int(rec["id"])] = rec
    return by_id


def contrato_ocr_sigue_elegible(
    supa, cid: int, *, solo_ti: bool, incluir_por_abrir: bool = False
) -> tuple[bool, str]:
    """SELECT fresco: Vigente + ventana abierta (+ TI si aplica)."""
    res = (
        supa.table("contratos")
        .select(
            "id,estado,fecha_ini_cotizacion,fecha_fin_cotizacion,"
            "clasificacion_contrato(categoria_it,relevancia_ia)"
        )
        .eq("id", cid)
        .limit(1)
        .execute()
    )
    if not res.data:
        return False, "no_encontrado"
    r = _aplanar_cl(res.data[0])
    if (r.get("estado") or "") != "Vigente":
        return False, f"estado={r.get('estado')}"
    now = datetime.now(timezone.utc)
    if not ventana_cotizacion_abierta(
        r, now, incluir_por_abrir=incluir_por_abrir
    ):
        fin = _parse_dt(r.get("fecha_fin_cotizacion"))
        if fin is None:
            return False, "ventana_null"
        if fin <= now:
            return False, "vencido"
        return False, "por_abrir"
    if solo_ti and not es_ti(r):
        return False, "no_ti"
    return True, "ok"


def _enriquecer_cola_ocr(supa, filas: list[dict]) -> list[dict]:
    ids = [int(r["id"]) for r in filas]
    extra: dict[int, dict] = {}
    for i in range(0, len(ids), 80):
        lote = ids[i:i + 80]
        res = (
            supa.table("contratos")
            .select(
                "id,estado,fecha_ini_cotizacion,fecha_fin_cotizacion,"
                "clasificacion_contrato(categoria_it,relevancia_ia)"
            )
            .in_("id", lote)
            .execute()
        )
        for row in res.data or []:
            extra[int(row["id"])] = _aplanar_cl(row)
    for r in filas:
        e = extra.get(int(r["id"])) or {}
        for k in (
            "estado",
            "fecha_ini_cotizacion",
            "fecha_fin_cotizacion",
            "categoria_it",
            "relevancia_ia",
        ):
            if r.get(k) in (None, "") and e.get(k) not in (None, ""):
                r[k] = e.get(k)
            elif k not in r:
                r[k] = e.get(k)
    return filas


def pendientes_ocr_paginas(
    supa,
    limit: int,
    *,
    solo_ti: bool = False,
    exigir_ventana: bool = True,
    incluir_por_abrir: bool = False,
) -> tuple[list[dict], dict]:
    """Vigentes+ventana+TI (opcional) con páginas imagen pendientes.

    Idempotente: `paginas_ocr_hechas` no se re-OCR. NULL en fecha_fin no gasta Flash.
    """
    if limit <= 0:
        limit = 10**9
    local = meta_local_por_id()
    cols = (
        "id,nro_contratacion,descripcion_contrato,entidad,fecha_publica,"
        "pdf_descargado,req_url,pdf_es_imagen,tdr_texto,pdf_archivo_id,pdf_nombre,"
        "estado,fecha_ini_cotizacion,fecha_fin_cotizacion,"
        "clasificacion_contrato(categoria_it,relevancia_ia)"
    )
    if columnas_extraccion_ok(supa):
        cols += (
            ",paginas_ocr_pendientes,paginas_ocr_hechas,tdr_tipo_extraccion,"
            "tdr_n_paginas,tdr_n_paginas_nativas,tdr_n_paginas_ocr"
        )
    out: list[dict] = []
    offset = 0
    while True:
        res = (
            supa.table("contratos")
            .select(cols)
            .eq("estado", "Vigente")
            .eq("pdf_descargado", True)
            .eq("pdf_es_imagen", True)
            .order("id", desc=True)
            .range(offset, offset + PAGE_DB - 1)
            .execute()
        )
        batch = res.data or []
        for r in batch:
            _aplanar_cl(r)
            cid = int(r["id"])
            loc = local.get(cid) or {}
            pend = _as_int_list(r.get("paginas_ocr_pendientes")) or _as_int_list(
                loc.get("paginas_ocr_pendientes")
            )
            hechas = _as_int_list(r.get("paginas_ocr_hechas")) or _as_int_list(
                loc.get("paginas_ocr_hechas")
            )
            pend = [p for p in pend if p not in hechas]
            if not pend:
                continue
            r["paginas_ocr_pendientes"] = pend
            r["paginas_ocr_hechas"] = hechas
            r["tdr_tipo_extraccion"] = (
                r.get("tdr_tipo_extraccion")
                or loc.get("tdr_tipo_extraccion")
            )
            r["tdr_n_paginas"] = r.get("tdr_n_paginas") or loc.get("tdr_n_paginas")
            r["n_paginas_nativas"] = (
                r.get("tdr_n_paginas_nativas")
                or loc.get("tdr_n_paginas_nativas")
            )
            r["n_paginas_ocr"] = (
                r.get("tdr_n_paginas_ocr")
                or loc.get("tdr_n_paginas_ocr")
                or len(pend) + len(hechas)
            )
            out.append(r)
        if len(batch) < PAGE_DB:
            break
        offset += PAGE_DB
    if not out:
        ids = [
            cid for cid, rec in local.items()
            if _as_int_list(rec.get("paginas_ocr_pendientes"))
        ]
        if not ids:
            return [], filtrar_ordenar_cola_ocr(
                [],
                solo_ti=solo_ti,
                exigir_ventana=exigir_ventana,
                incluir_por_abrir=incluir_por_abrir,
            )[1]
        filas = contratos_por_ids(supa, ids)
        by = {int(r["id"]): r for r in filas}
        extra = (
            supa.table("contratos")
            .select(
                "id,tdr_texto,pdf_archivo_id,pdf_nombre,pdf_es_imagen,"
                "estado,fecha_ini_cotizacion,fecha_fin_cotizacion,"
                "clasificacion_contrato(categoria_it,relevancia_ia)"
            )
            .in_("id", ids[:800])
            .execute()
        )
        extra_by = {int(r["id"]): _aplanar_cl(r) for r in (extra.data or [])}
        merged: list[dict] = []
        for cid in ids:
            if cid not in by and cid not in extra_by:
                continue
            row = {**(by.get(cid) or {}), **(extra_by.get(cid) or {})}
            rec = local[cid]
            pend = [
                p for p in _as_int_list(rec.get("paginas_ocr_pendientes"))
                if p not in _as_int_list(rec.get("paginas_ocr_hechas"))
            ]
            if not pend:
                continue
            row["id"] = cid
            row["paginas_ocr_pendientes"] = pend
            row["paginas_ocr_hechas"] = _as_int_list(rec.get("paginas_ocr_hechas"))
            row["tdr_tipo_extraccion"] = rec.get("tdr_tipo_extraccion")
            row["tdr_n_paginas"] = rec.get("tdr_n_paginas")
            row["n_paginas_nativas"] = rec.get("tdr_n_paginas_nativas")
            row["n_paginas_ocr"] = rec.get("tdr_n_paginas_ocr")
            merged.append(row)
        out = merged
    out = _enriquecer_cola_ocr(supa, out)
    filtradas, stats = filtrar_ordenar_cola_ocr(
        out,
        solo_ti=solo_ti,
        exigir_ventana=exigir_ventana,
        incluir_por_abrir=incluir_por_abrir,
    )
    return filtradas[:limit], stats


def guardar_ocr_progreso(
    supa,
    contrato: dict,
    tdr: str,
    pend: list[int],
    hechas: list[int],
) -> None:
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
    })
    _update_contrato(supa, cid, {
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


def rechunk_embed_pdf(supa, cid: int) -> None:
    from chunker_contratos import run_solo_pdf
    from generar_embeddings import run_gemini

    run_solo_pdf(supa, [cid], 0)
    run_gemini(supa, 0, fuente="pdf", ids=[cid], embed_mode="auto")


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


def escribir_ocr_log(supa, stats: dict) -> None:
    registrar_run(supa, PASO_OCR, stats)
    lines = [
        f"ts={datetime.now(timezone.utc).isoformat()}",
        *[f"{k}={v}" for k, v in stats.items()],
    ]
    texto = "\n".join(lines) + "\n"
    try:
        OCR_LOG.parent.mkdir(parents=True, exist_ok=True)
        OCR_LOG.write_text(texto, encoding="utf-8")
    except OSError as e:
        print(f"  [warn] no se pudo escribir {OCR_LOG}: {e}", flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        md = (
            "### OCR selectivo\n\n"
            f"- cola vigente+ventana+TI: **{stats.get('cola_contratos')}** "
            f"contratos / **{stats.get('cola_paginas')}** páginas\n"
            f"- OCR-eados: **{stats.get('ocr_contratos')}** contratos / "
            f"**{stats.get('ocr_paginas')}** páginas\n"
            f"- flash: {stats.get('flash_hoy')}/{stats.get('max_dia')}\n"
            f"- **motivo_parada={stats.get('motivo_parada')}**  "
            f"elapsed={stats.get('elapsed_s')}s  exit=0\n"
        )
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(md)
        except OSError:
            pass
    print(texto, flush=True)


def run_ocr_selectivo(
    supa,
    *,
    limit: int,
    ids: list[int],
    headed: bool,
    max_dia: int,
    dry_run: bool,
    solo_ti: bool = False,
    max_segundos: int = OCR_MAX_SEGUNDOS_DEFAULT,
    incluir_por_abrir: bool = False,
) -> None:
    cuota = cargar_cuota_ocr(supa)
    t0 = time.monotonic()
    exigir_ti = solo_ti and not ids
    print("=" * 60, flush=True)
    print("PASO C — OCR selectivo por página (append, sin reemplazar nativo)", flush=True)
    print(
        f"  fecha_lima={cuota['fecha']}  usadas={cuota['requests']}/{max_dia}  "
        f"usd_est={cuota['usd_est']:.4f}  "
        f"S/{float(cuota['usd_est']) * USD_PEN:.2f}  "
        f"rpm={OCR_RPM or '-'}  "
        f"solo_ti={exigir_ti}  incluir_por_abrir={incluir_por_abrir}  "
        f"max_segundos={max_segundos or 'off'}",
        flush=True,
    )
    print(
        "  filtros: estado=Vigente AND fecha_fin_cotizacion IS NOT NULL "
        "AND fecha_fin_cotizacion > now() "
        + (
            "(incluye por-abrir)"
            if incluir_por_abrir
            else "AND (fecha_ini_cotizacion IS NULL OR fecha_ini_cotizacion <= now())"
        )
        + "  (NULL no gasta Flash)",
        flush=True,
    )
    if not columnas_extraccion_ok(supa):
        print(
            "  [warn] columnas tdr_tipo_extraccion/paginas_ocr_* ausentes; "
            "la cola usa data/tdr_extraccion.jsonl "
            "(aplica tdr_extraccion_meta.sql + --sync-meta)",
            flush=True,
        )
    print("=" * 60, flush=True)

    def _log_final(
        motivo: str,
        *,
        ok_c: int = 0,
        pags_ok: int = 0,
        err: int = 0,
        cola_c: int = 0,
        cola_p: int = 0,
        ids_tocados: list[int] | None = None,
        cola_stats: dict | None = None,
        rest_c: int | None = None,
        rest_p: int | None = None,
    ) -> None:
        elapsed = int(time.monotonic() - t0)
        cs = cola_stats or {}
        stats = {
            "modo": "ocr_selectivo",
            "solo_ti": exigir_ti,
            "max_segundos": max_segundos,
            "cola_contratos": cola_c,
            "cola_paginas": cola_p,
            "cola_alta": cs.get("alta", ""),
            "cola_categoria_it": cs.get("categoria_it", ""),
            "cola_media": cs.get("media", ""),
            "cola_baja": cs.get("baja", ""),
            "descartados_no_ti": cs.get("no_ti", ""),
            "descartados_ventana_null": cs.get("ventana_null", ""),
            "descartados_vencidos": cs.get("vencidos", ""),
            "descartados_por_abrir": cs.get("por_abrir", ""),
            "ocr_contratos": ok_c,
            "ocr_paginas": pags_ok,
            "err": err,
            "flash_hoy": cuota["requests"],
            "max_dia": max_dia,
            "usd_est": round(float(cuota["usd_est"]), 6),
            "pen_est": round(float(cuota["usd_est"]) * USD_PEN, 4),
            "pendientes_contratos": rest_c if rest_c is not None else "",
            "pendientes_paginas": rest_p if rest_p is not None else "",
            "ids_tocados": ",".join(str(x) for x in (ids_tocados or [])),
            "motivo_parada": motivo,
            "elapsed_s": elapsed,
            "exit": 0,
        }
        print(f"\n{'=' * 60}", flush=True)
        print(
            f"OCR listo en {elapsed}s  contratos={ok_c} "
            f"paginas_ocr={pags_ok} err={err} "
            f"flash_hoy={cuota['requests']}/{max_dia}",
            flush=True,
        )
        print(f"  motivo_parada={motivo}  exit=0", flush=True)
        print("=" * 60, flush=True)
        escribir_ocr_log(supa, stats)
        escribir_resumen(supa, stats)

    if int(cuota["requests"]) >= max_dia:
        print("Tope diario ya alcanzado. Reanudar mañana.", flush=True)
        _log_final("cupo")
        return

    filas, cola_stats = pendientes_ocr_paginas(
        supa,
        10**9 if ids else limit,
        solo_ti=exigir_ti,
        exigir_ventana=True,
        incluir_por_abrir=incluir_por_abrir,
    )
    if ids:
        want = set(ids)
        filas = [r for r in filas if int(r["id"]) in want]

    cola_c = len(filas)
    cola_p = sum(len(r.get("paginas_ocr_pendientes") or []) for r in filas)
    print(
        f"  cola vigente+ventana+TI={cola_c} contratos  "
        f"paginas={cola_p}  "
        f"alta={cola_stats.get('alta')} categoria_it={cola_stats.get('categoria_it')} "
        f"media={cola_stats.get('media')} baja={cola_stats.get('baja')}",
        flush=True,
    )
    print(
        f"  descartados: no_ti={cola_stats.get('no_ti')} "
        f"ventana_null={cola_stats.get('ventana_null')} "
        f"vencidos={cola_stats.get('vencidos')} "
        f"por_abrir={cola_stats.get('por_abrir')} "
        f"no_vigente={cola_stats.get('no_vigente')}",
        flush=True,
    )
    for r in filas[:20]:
        print(
            f"    id={r['id']} ia={r.get('relevancia_ia') or '-'} "
            f"cat={r.get('categoria_it') or '-'} "
            f"ini={r.get('fecha_ini_cotizacion')} "
            f"fin={r.get('fecha_fin_cotizacion')} "
            f"tipo={r.get('tdr_tipo_extraccion')} "
            f"pend={len(r.get('paginas_ocr_pendientes') or [])}",
            flush=True,
        )
    if len(filas) > 20:
        print(f"    ... +{len(filas) - 20} contratos", flush=True)

    if dry_run:
        _log_final(
            "dry_run",
            cola_c=cola_c,
            cola_p=cola_p,
            cola_stats=cola_stats,
        )
        return
    if not filas:
        print("Nada que OCR-ear (idempotente).", flush=True)
        _log_final(
            "vacio",
            cola_c=cola_c,
            cola_p=cola_p,
            cola_stats=cola_stats,
            rest_c=0,
            rest_p=0,
        )
        return

    ok_c = 0
    pags_ok = 0
    err = 0
    ids_tocados: list[int] = []
    motivo_parada = "completo"
    http = SeaceHttp(headed=headed)
    try:
        for i, c in enumerate(filas, 1):
            cid = int(c["id"])
            entro_ocr = False
            try:
                if ocr_tiempo_agotado(t0, max_segundos):
                    raise CupoFlash(
                        f"tope {max_segundos}s de reloj",
                        motivo="tiempo",
                    )
                if ocr_sin_margen_contrato(t0, max_segundos):
                    raise CupoFlash(
                        "quedan <45s; no arrancar contrato",
                        motivo="tiempo",
                    )
                ok_el, razon = contrato_ocr_sigue_elegible(
                    supa, cid, solo_ti=exigir_ti,
                    incluir_por_abrir=incluir_por_abrir,
                )
                if not ok_el:
                    print(f"  skip id={cid} {razon}", flush=True)
                    continue
                entro_ocr = True
                row = ocr_contrato_selectivo(
                    http, supa, c, cuota, max_dia,
                    t0=t0, max_segundos=max_segundos,
                )
                nnew = len(row["nuevas"])
                pags_ok += nnew
                ok_c += 1
                if nnew:
                    ids_tocados.append(cid)
                    try:
                        rechunk_embed_pdf(supa, cid)
                    except Exception as e:
                        print(
                            f"  [warn] rechunk/embed id={cid}: {e}",
                            flush=True,
                        )
                imprimir_linea(
                    i, len(filas), cid, "OCR_OK",
                    f"nuevas={nnew} pend={row['pend']} "
                    f"flash={cuota['requests']}/{max_dia} "
                    f"S/{float(cuota['usd_est']) * USD_PEN:.2f}",
                )
            except CupoFlash as e:
                motivo_parada = getattr(e, "motivo", None) or "cupo"
                print(
                    f"  STOP OCR motivo_parada={motivo_parada}  {e}",
                    flush=True,
                )
                if entro_ocr:
                    if cid not in ids_tocados:
                        ids_tocados.append(cid)
                    try:
                        rechunk_embed_pdf(supa, cid)
                    except Exception as e2:
                        print(
                            f"  [warn] rechunk/embed id={cid}: {e2}",
                            flush=True,
                        )
                break
            except SinPdf as e:
                err += 1
                imprimir_linea(i, len(filas), cid, "SIN_PDF",
                               f"archivos={len(e.archivos)}")
                registrar_rechazo(
                    supa,
                    payload_rechazo(c, MOTIVO_SIN_PDF, {
                        "archivos": resumen_archivos(e.archivos),
                    }),
                    MOTIVO_SIN_PDF,
                    origen="pdf",
                )
            except Exception as e:
                err += 1
                imprimir_linea(i, len(filas), cid, f"FAIL ({e})")
                registrar_rechazo(
                    supa,
                    payload_rechazo(c, str(e)[:500]),
                    str(e),
                    origen="pdf",
                )
            time.sleep(DELAY_S)
    finally:
        http.close()

    rest, _st = pendientes_ocr_paginas(
        supa, 10**9, solo_ti=exigir_ti, exigir_ventana=True
    )
    rest_p = sum(len(r.get("paginas_ocr_pendientes") or []) for r in rest)
    print(
        f"  pendientes restantes: contratos={len(rest)} paginas={rest_p}",
        flush=True,
    )
    _log_final(
        motivo_parada,
        ok_c=ok_c,
        pags_ok=pags_ok,
        err=err,
        cola_c=cola_c,
        cola_p=cola_p,
        ids_tocados=ids_tocados,
        cola_stats=cola_stats,
        rest_c=len(rest),
        rest_p=rest_p,
    )


def guardar_ok(supa, row: dict) -> None:
    registrar_meta_local(row)
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
        payload["pdf_storage_path"] = row["pdf_storage_path"]
        payload["pdf_storage_at"] = datetime.now(timezone.utc).isoformat()
        if row.get("pdf_storage_bytes") is not None:
            payload["pdf_storage_bytes"] = int(row["pdf_storage_bytes"])
    _update_contrato(supa, row["id"], payload)


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
        payload["pdf_storage_path"] = meta["pdf_storage_path"]
        payload["pdf_storage_at"] = datetime.now(timezone.utc).isoformat()
        if meta.get("pdf_storage_bytes") is not None:
            payload["pdf_storage_bytes"] = int(meta["pdf_storage_bytes"])
    _update_contrato(supa, cid, payload)


def persistir_storage_si_hay(supa, cid: int, meta: dict) -> None:
    """Persiste solo columnas pdf_storage_* si el upload ya llenó meta."""
    path = meta.get("pdf_storage_path")
    if not path:
        return
    payload = {
        "pdf_storage_path": path,
        "pdf_storage_at": datetime.now(timezone.utc).isoformat(),
    }
    if meta.get("pdf_storage_bytes") is not None:
        payload["pdf_storage_bytes"] = int(meta["pdf_storage_bytes"])
    if meta.get("pdf_archivo_id") is not None:
        payload["_pdf_archivo_id"] = meta["pdf_archivo_id"]
    if meta.get("pdf_nombre"):
        payload["_pdf_nombre"] = str(meta["pdf_nombre"])[:500]
    _update_contrato(supa, cid, payload)


def guardar_sin_pdf(supa, cid: int) -> None:
    """No reintentar: no hay anexo PDF. tdr_texto queda null."""
    _update_contrato(supa, cid, {
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


def payload_rechazo(contrato: dict, motivo: str, extra: dict | None = None) -> dict:
    out = {
        "idContrato": int(contrato["id"]),
        "nroContratacion": contrato.get("nro_contratacion"),
        "desContratacion": contrato.get("descripcion_contrato"),
        "motivo": motivo,
    }
    if extra:
        out.update(extra)
    return out


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


def columnas_extraccion_ok(supa) -> bool:
    try:
        (
            supa.table("contratos")
            .select(",".join(COLS_EXTRACCION))
            .limit(1)
            .execute()
        )
        return True
    except Exception:
        return False


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
            _aplanar_cl(r)
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


def escribir_resumen(supa, stats: dict) -> None:
    """Log de corrida: OCR_PAGINAS_TOTAL para vigilar el free tier de Flash."""
    from datetime import datetime, timezone

    registrar_run(supa, PASO_PDF, stats)
    lines = [
        f"ts={datetime.now(timezone.utc).isoformat()}",
        *[f"{k}={v}" for k, v in stats.items()],
    ]
    texto = "\n".join(lines) + "\n"
    log = Path(__file__).parent / "data" / "ultima_pdf.txt"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(texto, encoding="utf-8")
    except OSError as e:
        print(f"  [warn] no se pudo escribir {log}: {e}", flush=True)
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
            limit=args.limit,
            ids=ids,
            headed=args.headed,
            max_dia=max_dia,
            dry_run=args.dry_run,
            solo_ti=args.solo_ti,
            max_segundos=args.max_segundos,
            incluir_por_abrir=args.incluir_por_abrir,
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
