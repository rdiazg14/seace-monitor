#!/usr/bin/env python3
"""
Fase 3.5 — Extraer TDR de contenedores no-PDF (vigentes TI/IA).

Cubre los anexos que NO son PDF y que el pipeline de PDF (`descargar_requerimiento`)
descarta por no tener magic `%PDF`: DOCX, ZIP (con hijos PDF/DOCX/XLSX/imagen) y,
si hay binario `unrar`/`7z`, RAR. El `.doc` binario (OLE2) se deja fuera por ahora.

Estrategia (híbrida, aprobada 19 sep):
  - DOCX  → python-docx (párrafos + tablas).
  - ZIP   → zipfile en memoria (sin extraer a disco; inmune a path traversal),
            clasifica cada hijo por magic bytes y extrae pdf/docx/xlsx/imagen.
  - RAR   → rarfile solo si hay backend; si no, rechazo `rar_sin_binario`.
  - .doc  → pospuesto (rechazo `doc_sin_soporte`).

El resultado confluye al flujo existente: escribe `contratos.tdr_texto` +
`tdr_tipo_extraccion=contenedor_*`, y dispara rechunk + embed (igual que el OCR).

Alcance: SOLO vigentes TI/IA sin TDR (coherente con el resto del pipeline).

Uso:
  uv run python extraer_contenedores.py --dry-run
  uv run python extraer_contenedores.py --limit 10
  uv run python extraer_contenedores.py --ids 6574,41042
"""
from __future__ import annotations

from seace_monitor.config import cargar_env

import argparse
import os
import re
import time
from collections import Counter

import httpx
from seace_monitor.supabase_client import crear_cliente

from seace_monitor.documents.container_service import (
    procesar_contenedor as _procesar_contenedor,
)
from seace_monitor.documents.pdf_extraction import limpiar_texto
from seace_monitor.documents.postprocess import rechunk_embed_pdf as _rechunk_embed_pdf
from seace_monitor.documents.repository import (
    contenedores_por_ids,
    vigentes_ti_sin_tdr,
)
from seace_monitor.documents.seace_files import (
    DEFAULT_DESCARGAR_URL,
    DEFAULT_LISTAR_URL,
    SeaceHttp,
    listar_archivos as listar_archivos_seace,
)
from seace_monitor.ocr.gemini_provider import (
    solicitar_ocr_gemini,
)
from seace_monitor.logging import PASO_CONTENEDORES, registrar_run

# ── Cargar .env ────────────────────────────────────────────────────────────────
cargar_env()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
LISTAR_URL = os.environ.get("LISTAR_URL", DEFAULT_LISTAR_URL).strip()
DESCARGAR_URL = os.environ.get("DESCARGAR_URL", DEFAULT_DESCARGAR_URL).strip()
DELAY_S = 0.35

def parse_ids(raw: str) -> list[int]:
    return [int(part) for part in re.split(r"[,\s]+", raw.strip()) if part]


def listar_archivos(http: SeaceHttp, cid: int) -> tuple[str, list]:
    return listar_archivos_seace(http, cid, LISTAR_URL)


def ocr_pagina_gemini(img_bytes: bytes, mime: str = "image/jpeg") -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY ausente; no se puede hacer OCR")
    return limpiar_texto(
        solicitar_ocr_gemini(httpx, img_bytes, mime, GEMINI_API_KEY)
    )


def rechunk_embed_pdf(supa, cid: int) -> None:
    _rechunk_embed_pdf(supa, cid, api_key=GEMINI_API_KEY)


def procesar_contenedor(http: SeaceHttp, supa, c: dict, dry_run: bool) -> str:
    return _procesar_contenedor(
        http,
        supa,
        c,
        dry_run,
        listar_archivos=listar_archivos,
        descargar_url=DESCARGAR_URL,
        ocr_page=ocr_pagina_gemini if GEMINI_API_KEY else None,
        rechunk_embed=rechunk_embed_pdf,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = todos")
    ap.add_argument("--ids", default="", help="Ids fijos separados por coma")
    ap.add_argument("--dry-run", action="store_true", help="No escribe en BD")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    supa = crear_cliente()
    ids = parse_ids(args.ids)

    if ids:
        filas = contenedores_por_ids(supa, ids)
    else:
        limit = args.limit if args.limit > 0 else 10**9
        filas = vigentes_ti_sin_tdr(supa, limit)

    print("=" * 60, flush=True)
    print("Fase 3.5 — Contenedores no-PDF (vigentes TI/IA)", flush=True)
    print(
        f"  dry-run={args.dry_run}  ids={ids or '-'}  cola={len(filas)}  "
        f"GEMINI_API_KEY set={bool(GEMINI_API_KEY)}",
        flush=True,
    )
    print("=" * 60, flush=True)

    if not filas:
        print("Nada que hacer (sin vigentes TI/IA sin TDR).", flush=True)
        return

    estados: Counter[str] = Counter()
    ok = 0
    t0 = time.time()
    http = SeaceHttp(headed=args.headed)
    try:
        for i, c in enumerate(filas, 1):
            cid = int(c["id"])
            desc = (c.get("descripcion_contrato") or "")[:50]
            try:
                estado = procesar_contenedor(http, supa, c, args.dry_run)
            except Exception as e:
                estado = f"error({str(e)[:40]})"
            estados[estado] += 1
            if estado.startswith("ok_"):
                ok += 1
            print(f"  [{i}/{len(filas)}] id={cid} {estado}  {desc}", flush=True)
            time.sleep(DELAY_S)
    finally:
        http.close()

    elapsed = time.time() - t0
    print(f"\n{'='*60}", flush=True)
    print(f"Contenedores listos en {elapsed:.0f}s  ok={ok}", flush=True)
    for k, v in sorted(estados.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}", flush=True)
    print("=" * 60, flush=True)
    registrar_run(
        supa,
        PASO_CONTENEDORES,
        {
            "ok": ok,
            "cola": len(filas),
            "elapsed_s": round(elapsed, 1),
            "estados": dict(estados),
            "dry_run": args.dry_run,
            "ids": ids or None,
        },
    )


if __name__ == "__main__":
    main()
