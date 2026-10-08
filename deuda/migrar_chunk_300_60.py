#!/usr/bin/env python3
"""Migración de chunking 500/0 -> 300/60 (overlap) SOLO en postulables TI/IA.

El A/B offline (eval_chunking.py) dio ganadora a la variante 300_60:
  success@10 0.933 -> 0.967,  P@5 0.620 -> 0.667,  P@10 0.511 -> 0.571,
  recall (relevantes) 190 -> 188 (ruido de re-frontera, mitigado por overlap).

Para no gastar créditos en todo el corpus, se migra SOLO lo que
importa ahora (postulables TI/IA con TDR). El resto queda en `chunk_version`
= '500_0' y se completa cuando haya créditos. Los TDR nuevos ya nacen en
300/60 vía ``chunks_de_pdf`` (DATA-001).

Doble escritura (regla de paridad v2=v3): cada chunk nuevo se embebe en
embedding_v3 (Qwen, espacio activo) Y embedding_v2 (Gemini, reserva).
Si Qwen agota cupo el contrato NO se migra (conserva chunks viejos);
si Gemini agota cupo el swap se hace con embedding_v2 NULL y el
diferencial diario lo iguala al recargar.

Estado en BD: contratos.chunk_version
  '500_0'  (default)  -> legacy, pendiente de migrar
  '300_60'            -> re-chunkeado a 300/60 y embebido

Atómico por contrato: primero se re-chunkea y se embebe el TDR (el paso
caro y propenso a 429). Recién cuando el espacio activo está embebido se
borran sus chunks fuente=pdf viejos y se insertan los nuevos. Si Qwen
devuelve 429 (cupo), el contrato conserva sus chunks 500/0 intactos y el
script se detiene; los ya migrados quedan marcados y el resto es
reanudable re-ejecutando el mismo comando.

Uso:
  uv run python migrar_chunk_300_60.py --dry-run            # alcance + costo
  uv run python migrar_chunk_300_60.py --limit 20           # migrar 20 (control de cupo)
  uv run python migrar_chunk_300_60.py --ids 87164,87001    # ids específicos
"""
from __future__ import annotations
import _bootstrap  # noqa: F401

from seace_monitor.config import cargar_env

import argparse
import os
import time
from pathlib import Path

import httpx
from supabase import create_client

from seace_monitor.rag import chunking as cc
from seace_monitor.embeddings.gemini_provider import (
    GEMINI_EMBED_MODEL,
    QuotaExceeded,
    solicitar_embeddings_gemini,
)
from seace_monitor.embeddings.openai_provider import solicitar_embeddings_openai
from seace_monitor.embeddings.preparation import (
    EMBED_STATS,
    texto_para_embed,
    vec_literal,
)
from seace_monitor.embeddings.service import BATCH_GEMINI, DELAY_GEMINI_S
from seace_monitor.gemini import EMBED_USD_PER_M
from seace_monitor.rag.repository import reemplazar_chunks_contrato
from seace_monitor.logging import PASO_EMBEDDING, registrar_evento, registrar_run

cargar_env()

TARGET = cc.TARGET_PDF
OVERLAP = cc.OVERLAP_PDF
CHUNK_VERSION_DEST = cc.CHUNK_VERSION_PDF
PAGE = 1_000
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
QWEN_EMBED_URL = "https://maas.qwencloudapi.com/compatible-mode/v1/embeddings"
QWEN_EMBED_MODEL = "text-embedding-v4"
QWEN_EMBED_USD_PER_M = 0.07
BATCH_QWEN = 10


def split_parrafos_overlap(
    texto: str, target_tokens: int, overlap_tokens: int
) -> list[str]:
    return cc.split_parrafos_overlap(texto, target_tokens, overlap_tokens)


def chunks_pdf_300_60(c: dict) -> list[dict]:
    """Chunks fuente=pdf del TDR a 300/60; hoy coincide con cc.chunks_de_pdf."""
    return cc.chunks_de_pdf(c)


def cargar_cola(supa, incluir_por_abrir: bool) -> list[dict]:
    """Postulables TI/IA con TDR y chunk_version != 300_60, ordenados por cierre."""
    cond = (
        "es_postulable.eq.true,es_por_abrir.eq.true"
        if incluir_por_abrir
        else "es_postulable.eq.true"
    )
    ids: list[int] = []
    offset = 0
    while True:
        res = (
            supa.table("v_contratos_estado")
            .select("id")
            .or_(cond)
            .order("id")
            .range(offset, offset + PAGE - 1)
            .execute()
        )
        batch = res.data or []
        ids.extend(int(r["id"]) for r in batch)
        if len(batch) < PAGE:
            break
        offset += PAGE

    cols = (
        "id,nro_contratacion,descripcion_contrato,descripcion,entidad,objeto,"
        "tdr_texto,chunk_version,fecha_fin_cotizacion"
    )
    by_id: dict[int, dict] = {}
    for i in range(0, len(ids), 80):
        lote = ids[i:i + 80]
        res = supa.table("contratos").select(cols).in_("id", lote).execute()
        for r in res.data or []:
            by_id[int(r["id"])] = r

    out: list[dict] = []
    for cid in ids:
        c = by_id.get(cid)
        if not c:
            continue
        if not (c.get("tdr_texto") or "").strip():
            continue
        if (c.get("chunk_version") or "500_0") == CHUNK_VERSION_DEST:
            continue
        out.append(c)
    out.sort(key=lambda r: (r.get("fecha_fin_cotizacion") or "9999", int(r["id"])))
    return out


def max_chunk_index_api(supa, cid: int) -> int:
    res = (
        supa.table("chunks_tdr")
        .select("chunk_index")
        .eq("contrato_id", cid)
        .eq("fuente", "api")
        .order("chunk_index", desc=True)
        .limit(1)
        .execute()
    )
    return int(res.data[0]["chunk_index"]) if res.data else -1


def insertar_pdf(supa, chunks: list[dict]) -> None:
    if not chunks:
        return
    supa.table("chunks_tdr").upsert(
        chunks, on_conflict="contrato_id,chunk_index"
    ).execute()


def marcar_version(supa, cid: int) -> None:
    supa.table("contratos").update({"chunk_version": CHUNK_VERSION_DEST}).eq(
        "id", cid
    ).execute()


def estimar_costo(chunks: list[dict]) -> dict:
    chars = sum(len((c.get("chunk_embed_text") or "")) for c in chunks)
    tokens = chars / 4.0
    usd = tokens / 1_000_000.0 * EMBED_USD_PER_M
    return {"chunks": len(chunks), "chars": chars, "tokens": int(tokens), "usd": usd}


def main() -> int:
    ap = argparse.ArgumentParser(description="Migrar chunking 500/0 -> 300/60 en postulables TI/IA")
    ap.add_argument("--dry-run", action="store_true", help="Lista + costo, sin tocar BD ni Gemini")
    ap.add_argument("--limit", type=int, default=0, help="Tope de contratos (0 = todos)")
    ap.add_argument("--ids", default="", help="Ids fijos separados por coma")
    ap.add_argument("--incluir-por-abrir", action="store_true", help="Incluye es_por_abrir además de postulables")
    ap.add_argument("--delay", type=float, default=DELAY_GEMINI_S, help="Pausa entre lotes de embedding")
    ap.add_argument("--batch", type=int, default=BATCH_GEMINI, help="Tamaño de lote de embedding")
    args = ap.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SystemExit("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY no encontrados")
    if not GEMINI_API_KEY:
        raise SystemExit("ERROR: GEMINI_API_KEY no encontrado")
    if not DASHSCOPE_API_KEY:
        raise SystemExit("ERROR: DASHSCOPE_API_KEY no encontrado (escribe embedding_v3)")

    supa = create_client(SUPABASE_URL, SUPABASE_KEY)

    ids_fijos = [int(x) for x in args.ids.replace(" ", "").split(",") if x] if args.ids else []
    if ids_fijos:
        cols = (
            "id,nro_contratacion,descripcion_contrato,descripcion,entidad,objeto,"
            "tdr_texto,chunk_version,fecha_fin_cotizacion"
        )
        cola: list[dict] = []
        for i in range(0, len(ids_fijos), 80):
            lote = ids_fijos[i:i + 80]
            res = supa.table("contratos").select(cols).in_("id", lote).execute()
            for r in res.data or []:
                if (r.get("tdr_texto") or "").strip():
                    cola.append(r)
    else:
        cola = cargar_cola(supa, args.incluir_por_abrir)

    if args.limit > 0:
        cola = cola[: args.limit]

    print("=" * 64, flush=True)
    print(
        f"Migración chunking {TARGET}/{OVERLAP}  postulables TI/IA  "
        f"(incluir_por_abrir={args.incluir_por_abrir})",
        flush=True,
    )
    print(f"  en cola: {len(cola)} contratos (sin chunk_version={CHUNK_VERSION_DEST})", flush=True)
    print("=" * 64, flush=True)

    if not cola:
        print("Nada que hacer.", flush=True)
        return 0

    # Pre-cálculo de chunks (local, gratis) para el reporte de costo.
    total_chunks = 0
    total_chars = 0
    for c in cola:
        chs = chunks_pdf_300_60(c)
        total_chunks += len(chs)
        total_chars += sum(len((x.get("chunk_embed_text") or "")) for x in chs)
    est_tokens = total_chars / 4.0
    est_usd = est_tokens / 1_000_000.0 * (EMBED_USD_PER_M + QWEN_EMBED_USD_PER_M)
    print(f"  estimación: chunks={total_chunks:,}  chars={total_chars:,}  "
          f"tokens~{est_tokens:,.0f}x2 espacios  costo~USD {est_usd:.4f}", flush=True)
    for c in cola:
        chs = chunks_pdf_300_60(c)
        print(
            f"  - id={c['id']} nro={c.get('nro_contratacion')} "
            f"tdr_chars={len((c.get('tdr_texto') or ''))} chunks={len(chs)}",
            flush=True,
        )

    if args.dry_run:
        print("dry-run: sin migrar.", flush=True)
        return 0

    migrados = 0
    cupo = False
    tok_qwen = 0
    tok_gem = 0
    t0 = time.time()
    tokens_ini = int(EMBED_STATS.get("tokens_api") or 0)
    with httpx.Client() as http:
        for i, c in enumerate(cola, 1):
            cid = int(c["id"])
            new_chunks = chunks_pdf_300_60(c)
            if not new_chunks:
                print(f"[{i}/{len(cola)}] id={cid} sin chunks (tdr vacío), omitido", flush=True)
                continue
            # 1) Embeber ambos espacios ANTES del swap (paridad v2=v3, DATA-001).
            #    v3 = espacio activo del corpus: si Qwen se queda sin cupo el
            #    contrato NO se migra (sus chunks viejos siguen recuperables).
            #    v2 = reserva: si Gemini agota cupo la columna queda NULL y el
            #    diferencial diario la iguala al recargar créditos.
            texts = [texto_para_embed(r, "auto") for r in new_chunks]
            n_ok = 0
            tok_qwen_ini = int(EMBED_STATS.get("tokens_api") or 0)
            try:
                for j in range(0, len(new_chunks), BATCH_QWEN):
                    lote_t = texts[j:j + BATCH_QWEN]
                    vecs = solicitar_embeddings_openai(
                        http, lote_t, DASHSCOPE_API_KEY, fail_fast=True,
                        url=QWEN_EMBED_URL, modelo=QWEN_EMBED_MODEL,
                        dimensiones=1536, batch_max=BATCH_QWEN, proveedor="qwen",
                    )
                    for r, v in zip(new_chunks[j:j + BATCH_QWEN], vecs):
                        r["embedding_v3"] = vec_literal(v)
                    n_ok += len(lote_t)
                    if j + BATCH_QWEN < len(new_chunks):
                        time.sleep(args.delay)
            except QuotaExceeded as e:
                print(f"  STOP 429 qwen en id={cid} (v3 {n_ok}/{len(new_chunks)}). {e}", flush=True)
                print(f"  pendientes={[int(x['id']) for x in cola[i - 1:]]}", flush=True)
                cupo = True
                break
            except Exception as e:
                print(f"  [error] id={cid} embed v3: {e}", flush=True)
                continue
            tok_qwen += int(EMBED_STATS.get("tokens_api") or 0) - tok_qwen_ini

            v2_parcial = False
            n_v2 = 0
            tok_gem_ini = int(EMBED_STATS.get("tokens_api") or 0)
            try:
                for j in range(0, len(new_chunks), args.batch):
                    lote_t = texts[j:j + args.batch]
                    vecs = solicitar_embeddings_gemini(
                        http, lote_t, GEMINI_API_KEY, fail_fast=True
                    )
                    for r, v in zip(new_chunks[j:j + args.batch], vecs):
                        r["embedding_v2"] = vec_literal(v)
                    n_v2 += len(lote_t)
                    if j + args.batch < len(new_chunks):
                        time.sleep(args.delay)
            except QuotaExceeded as e:
                v2_parcial = True
                print(
                    f"  [aviso] v2 cupo gemini en id={cid} ({n_v2}/{len(new_chunks)}); "
                    "los NULL los iguala el diferencial al recargar.",
                    flush=True,
                )
            except Exception as e:
                v2_parcial = True
                print(f"  [aviso] id={cid} embed v2: {e}; columna queda NULL", flush=True)
            tok_gem += int(EMBED_STATS.get("tokens_api") or 0) - tok_gem_ini

            # 2) Recién ahora reemplazar (upsert primero, limpieza de restos
            #    después — FIX-003; un fallo intermedio jamás deja el corpus vacío).
            offset = max_chunk_index_api(supa, cid) + 1
            for k, r in enumerate(new_chunks):
                r["chunk_index"] = offset + k
            reemplazar_chunks_contrato(supa, cid, new_chunks, fuente="pdf")
            marcar_version(supa, cid)
            migrados += 1
            cost = estimar_costo(new_chunks)
            registrar_evento(
                supa,
                cid,
                "migrado_300_60",
                n_chunks_pdf=cost["chunks"],
                chars_tdr=cost["chars"],
                tokens_est=cost["tokens"],
                costo_usd=cost["usd"],
                chunk_version=CHUNK_VERSION_DEST,
                detalle={
                    "target": TARGET,
                    "overlap": OVERLAP,
                    "v3": n_ok,
                    "v2": n_v2,
                    "v2_parcial": v2_parcial,
                },
            )
            print(
                f"[{i}/{len(cola)}] id={cid} chunks={len(new_chunks)} "
                f"offset={offset} v3={n_ok} v2={n_v2} -> {CHUNK_VERSION_DEST}",
                flush=True,
            )

    elapsed = time.time() - t0
    tok_run = int(EMBED_STATS.get("tokens_api") or 0) - tokens_ini
    # uso_ia separado por proveedor: cada espacio tiene su tarifa.
    for modelo, tokens, usd_per_m in (
        (GEMINI_EMBED_MODEL, tok_gem, EMBED_USD_PER_M),
        (QWEN_EMBED_MODEL, tok_qwen, QWEN_EMBED_USD_PER_M),
    ):
        if not tokens:
            continue
        try:
            supa.table("uso_ia").insert({
                "componente": "embedding",
                "modelo": modelo,
                "tokens_prompt": tokens,
                "tokens_total": tokens,
                "costo_usd": round(tokens / 1_000_000.0 * usd_per_m, 8),
                "cache_hit": False,
                "detalle": {
                    "n_contratos": migrados,
                    "chunk_version": CHUNK_VERSION_DEST,
                    "target": TARGET,
                    "overlap": OVERLAP,
                },
            }).execute()
        except Exception as e:
            print(f"  [warn] log_uso_ia {modelo}: {e}", flush=True)
    costo_total = (
        tok_gem / 1e6 * EMBED_USD_PER_M + tok_qwen / 1e6 * QWEN_EMBED_USD_PER_M
    )
    registrar_run(
        supa,
        PASO_EMBEDDING,
        {
            "migrados": migrados,
            "cola": len(cola),
            "tokens_api": tok_run,
            "tokens_gemini": tok_gem,
            "tokens_qwen": tok_qwen,
            "costo_usd": round(costo_total, 8),
            "chunk_version": CHUNK_VERSION_DEST,
            "cupo": cupo,
            "elapsed_s": round(elapsed, 1),
        },
    )
    print(f"\nlisto migrados={migrados}/{len(cola)} en {elapsed:.0f}s cupo={cupo}", flush=True)
    return 1 if cupo else 0


if __name__ == "__main__":
    raise SystemExit(main())
