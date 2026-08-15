#!/usr/bin/env python3
"""
Fase 4 — Genera embeddings bge-base-en-v1.5 (768 dims) para chunks_tdr.

Llama a POST {EMBED_URL}/embed  (Cloudflare Worker)
Body: { "texts": ["...", ...] }  máx 20
Response: { "embeddings": [[...], ...] }

Uso: python generar_embeddings.py [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import httpx
from supabase import create_client

_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
EMBED_URL = os.environ.get(
    "EMBED_URL",
    "https://seace-ai-proxy.rdiazg14.workers.dev/embed",
)

PAGE = 1_000
BATCH = 20
DELAY_S = 1.0
MAX_CHARS = 2_000  # bge-base-en-v1.5 max 512 tokens


def vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


def embed_lote(client: httpx.Client, texts: list[str]) -> list[list[float]]:
    waits = [2.0, 4.0, 8.0]
    last_err: Exception | None = None
    for attempt, wait in enumerate([0.0] + waits):
        if wait:
            time.sleep(wait)
        try:
            r = client.post(EMBED_URL, json={"texts": texts}, timeout=60.0)
            r.raise_for_status()
            body = r.json()
            embs = body.get("embeddings")
            if not isinstance(embs, list) or len(embs) != len(texts):
                raise RuntimeError(f"respuesta inesperada: keys={list(body)[:8]} n={len(embs) if isinstance(embs, list) else None}")
            if embs and len(embs[0]) != 768:
                raise RuntimeError(f"dimensión {len(embs[0])} != 768")
            return embs
        except Exception as e:
            last_err = e
            print(f"    [retry {attempt}] {e}", flush=True)
    raise RuntimeError(f"embed_lote falló: {last_err}")


def chunks_sin_embedding(supa, limit: int) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        take = PAGE if not limit else min(PAGE, limit - len(out))
        if take <= 0:
            break
        res = (
            supa.table("chunks_tdr")
            .select("id, contrato_id, chunk_index, tipo, texto")
            .is_("embedding", "null")
            .order("id")
            .range(offset, offset + take - 1)
            .execute()
        )
        batch = res.data or []
        out.extend(batch)
        if len(batch) < take:
            break
        offset += take
        if limit and len(out) >= limit:
            break
    return out[:limit] if limit else out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = todos")
    args = ap.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SystemExit("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY no encontrados")

    supa = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("=" * 60, flush=True)
    print("FASE 4 — Embeddings bge-base-en-v1.5", flush=True)
    print(f"  endpoint: {EMBED_URL}", flush=True)
    print("=" * 60, flush=True)

    pendientes = chunks_sin_embedding(supa, args.limit)
    total = len(pendientes)
    print(f"Chunks sin embedding: {total:,}", flush=True)
    if total == 0:
        print("Nada que hacer.", flush=True)
        return

    t0 = time.time()
    ok = 0
    errores = 0

    with httpx.Client() as http:
        for i in range(0, total, BATCH):
            lote = pendientes[i:i + BATCH]
            texts = [(row.get("texto") or "")[:MAX_CHARS] for row in lote]
            try:
                embs = embed_lote(http, texts)
                updates = [
                    {
                        "id": row["id"],
                        "contrato_id": row["contrato_id"],
                        "chunk_index": row["chunk_index"],
                        "tipo": row["tipo"],
                        "texto": row["texto"],
                        "embedding": vec_literal(vec),
                    }
                    for row, vec in zip(lote, embs)
                ]
                supa.table("chunks_tdr").upsert(updates, on_conflict="id").execute()
                ok += len(lote)
            except Exception as e:
                errores += len(lote)
                print(f"  [error] lote {i}-{i+len(lote)}: {e}", flush=True)

            done = min(i + BATCH, total)
            if done % 100 < BATCH or done == total:
                elapsed = time.time() - t0
                print(f"  [{done}/{total}] ok={ok} err={errores} {elapsed:.0f}s", flush=True)
            time.sleep(DELAY_S)

    elapsed = time.time() - t0
    con_emb = (
        supa.table("chunks_tdr")
        .select("id", count="exact")
        .not_.is_("embedding", "null")
        .limit(1)
        .execute()
    )
    print(f"\n{'='*60}", flush=True)
    print(f"Fase 4 completada en {elapsed:.0f}s", flush=True)
    print(f"  Procesados OK : {ok:,}", flush=True)
    print(f"  Errores       : {errores:,}", flush=True)
    print(f"  Con embedding : {con_emb.count:,}", flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    main()
