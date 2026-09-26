#!/usr/bin/env python3
"""Migración de chunking 500/0 -> 300/60 (overlap) SOLO en postulables TI/IA.

El A/B offline (eval_chunking.py) dio ganadora a la variante 300_60:
  success@10 0.933 -> 0.967,  P@5 0.620 -> 0.667,  P@10 0.511 -> 0.571,
  recall (relevantes) 190 -> 188 (ruido de re-frontera, mitigado por overlap).

Para no gastar créditos de Gemini en todo el corpus, se migra SOLO lo que
importa ahora (postulables TI/IA con TDR). El resto queda en `chunk_version`
= '500_0' y se completa cuando haya créditos.

Estado en BD: contratos.chunk_version
  '500_0'  (default)  -> legacy, pendiente de migrar
  '300_60'            -> re-chunkeado a 300/60 y embebido

Atómico por contrato: primero se re-chunkea y se embebe el TDR (el paso
caro y propenso a 429). Recién cuando TODO el contrato está embebido se
borran sus chunks fuente=pdf viejos y se insertan los nuevos con
embedding_v2. Si Gemini devuelve 429 (cupo), el contrato conserva sus
chunks 500/0 intactos y el script se detiene; los ya migrados quedan
marcados y el resto es reanudable re-ejecutando el mismo comando.

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
from seace_monitor.embeddings.preparation import (
    EMBED_STATS,
    texto_para_embed,
    vec_literal,
)
from seace_monitor.embeddings.service import BATCH_GEMINI, DELAY_GEMINI_S
from seace_monitor.gemini import EMBED_USD_PER_M
from seace_monitor.logging import PASO_EMBEDDING, registrar_evento, registrar_run

cargar_env()

TARGET = 300
OVERLAP = 60
CHUNK_VERSION_DEST = "300_60"
PAGE = 1_000
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")


def split_parrafos_overlap(
    texto: str, target_tokens: int, overlap_tokens: int
) -> list[str]:
    if overlap_tokens <= 0:
        return cc.split_por_parrafos(texto, target_tokens)
    partes = [p.strip() for p in texto.replace("\r\n", "\n").split("\n") if p.strip()]
    if not partes:
        return [texto.strip()] if texto.strip() else []
    chunks: list[str] = []
    buf: list[str] = []
    buf_tok = 0
    for p in partes:
        pt = cc.approx_tokens(p)
        buf.append(p)
        buf_tok += pt
        if buf_tok >= target_tokens:
            chunks.append("\n".join(buf))
            ov: list[str] = []
            ov_tok = 0
            for pp in reversed(buf):
                ov_tok += cc.approx_tokens(pp)
                ov.append(pp)
                if ov_tok >= overlap_tokens:
                    break
            ov.reverse()
            buf = ov
            buf_tok = ov_tok
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def chunks_pdf_300_60(c: dict) -> list[dict]:
    """Chunks fuente=pdf del TDR a target=300 overlap=60 (espejo de chunks_de_pdf)."""
    tdr = (c.get("tdr_texto") or "").strip()
    if not tdr:
        return []
    cid = int(c["id"])
    partes = (
        split_parrafos_overlap(tdr, TARGET, OVERLAP)
        if cc.approx_tokens(tdr) > cc.MAX_TOKENS_ANTES_SPLIT
        else [tdr]
    )
    meta = cc.meta_de_contrato(c)
    out: list[dict] = []
    for i, parte in enumerate(partes):
        texto = cc.con_contexto_pdf(c, parte)
        row = {
            "contrato_id": cid,
            "chunk_index": 0,  # se reasigna al offset real antes de insertar
            "tipo": "TDR PDF" if len(partes) == 1 else f"TDR PDF ({i + 1}/{len(partes)})",
            "texto": texto,
            "fuente": "pdf",
            "chunk_embed_text": cc.embed_text_pdf(c, texto),
        }
        row.update(meta)
        out.append(row)
    return out


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


def borrar_pdf(supa, cid: int) -> None:
    supa.table("chunks_tdr").delete().eq("contrato_id", cid).eq("fuente", "pdf").execute()


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
    est_usd = est_tokens / 1_000_000.0 * EMBED_USD_PER_M
    print(f"  estimación: chunks={total_chunks:,}  chars={total_chars:,}  "
          f"tokens~{est_tokens:,.0f}  costo~USD {est_usd:.4f}", flush=True)
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
    t0 = time.time()
    tokens_ini = int(EMBED_STATS.get("tokens_api") or 0)
    with httpx.Client() as http:
        for i, c in enumerate(cola, 1):
            cid = int(c["id"])
            new_chunks = chunks_pdf_300_60(c)
            if not new_chunks:
                print(f"[{i}/{len(cola)}] id={cid} sin chunks (tdr vacío), omitido", flush=True)
                continue
            # 1) Embeber TODO el contrato (paso caro). fail_fast: 429 => QuotaExceeded.
            embebido = True
            n_ok = 0
            try:
                for j in range(0, len(new_chunks), args.batch):
                    lote = new_chunks[j:j + args.batch]
                    texts = [texto_para_embed(r, "auto") for r in lote]
                    vecs = solicitar_embeddings_gemini(
                        http, texts, GEMINI_API_KEY, fail_fast=True
                    )
                    for r, v in zip(lote, vecs):
                        r["embedding_v2"] = vec_literal(v)
                    n_ok += len(lote)
                    if j + args.batch < len(new_chunks):
                        time.sleep(args.delay)
            except QuotaExceeded as e:
                print(f"  STOP 429 en id={cid} (embebidos {n_ok}/{len(new_chunks)}). {e}", flush=True)
                print(f"  pendientes={[int(x['id']) for x in cola[i - 1:]]}", flush=True)
                cupo = True
                break
            except Exception as e:
                print(f"  [error] id={cid}: {e}", flush=True)
                continue

            # 2) Recién ahora reemplazar (borrar viejos + insertar nuevos con offset).
            offset = max_chunk_index_api(supa, cid) + 1
            for k, r in enumerate(new_chunks):
                r["chunk_index"] = offset + k
            borrar_pdf(supa, cid)
            insertar_pdf(supa, new_chunks)
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
                detalle={"target": TARGET, "overlap": OVERLAP},
            )
            print(
                f"[{i}/{len(cola)}] id={cid} chunks={len(new_chunks)} "
                f"offset={offset} -> {CHUNK_VERSION_DEST}",
                flush=True,
            )

    elapsed = time.time() - t0
    tok_run = int(EMBED_STATS.get("tokens_api") or 0) - tokens_ini
    if tok_run > 0:
        try:
            supa.table("uso_ia").insert({
                "componente": "embedding",
                "modelo": GEMINI_EMBED_MODEL,
                "tokens_prompt": tok_run,
                "tokens_total": tok_run,
                "costo_usd": round(tok_run / 1_000_000.0 * EMBED_USD_PER_M, 8),
                "cache_hit": False,
                "detalle": {
                    "n_contratos": migrados,
                    "chunk_version": CHUNK_VERSION_DEST,
                    "target": TARGET,
                    "overlap": OVERLAP,
                },
            }).execute()
        except Exception as e:
            print(f"  [warn] log_uso_ia migración: {e}", flush=True)
    registrar_run(
        supa,
        PASO_EMBEDDING,
        {
            "migrados": migrados,
            "cola": len(cola),
            "tokens_api": tok_run,
            "costo_usd": round(tok_run / 1_000_000.0 * EMBED_USD_PER_M, 8),
            "chunk_version": CHUNK_VERSION_DEST,
            "cupo": cupo,
            "elapsed_s": round(elapsed, 1),
        },
    )
    print(f"\nlisto migrados={migrados}/{len(cola)} en {elapsed:.0f}s cupo={cupo}", flush=True)
    return 1 if cupo else 0


if __name__ == "__main__":
    raise SystemExit(main())
