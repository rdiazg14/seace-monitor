#!/usr/bin/env python3
"""
Embeddings para chunks_tdr.

  bge    (default, cron v1)  → columna embedding(768)  WHERE embedding IS NULL
  gemini (--backend gemini)  → columna embedding_v2(1536) WHERE embedding_v2 IS NULL
                               SOLO chunks de contratos Vigente.
                               No toca embedding(768). Idempotente.

Uso:
  python generar_embeddings.py [--limit N]
  python generar_embeddings.py --backend gemini [--limit N]
  python generar_embeddings.py --backend gemini --cobertura
  python generar_embeddings.py --backend gemini --auth-check
"""
from __future__ import annotations

import argparse
import math
import os
import time
from pathlib import Path

import httpx
from supabase import create_client

from chunker_contratos import cuerpo_chunk

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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_EMBED_MODEL = "gemini-embedding-001"
GEMINI_EMBED_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_EMBED_MODEL}:batchEmbedContents"
)
GEMINI_DIM = 1536

PAGE = 1_000
BATCH = 20
DELAY_S = 1.0
MAX_CHARS = 2_000  # bge-base-en-v1.5 max 512 tokens

BATCH_GEMINI = 16
DELAY_GEMINI_S = 0.4
MAX_CHARS_GEMINI = 8_000  # ~2k tokens
GEMINI_BACKOFF = (2.0, 4.0, 8.0, 16.0, 32.0, 64.0)


class QuotaExceeded(RuntimeError):
    """429 de Gemini. El caller debe PARAR; no reintentar."""


EMBED_STATS: dict[str, int] = {
    "requests": 0,
    "texts": 0,
    "chars": 0,
    "tokens_api": 0,
}


def reset_embed_stats() -> None:
    for k in EMBED_STATS:
        EMBED_STATS[k] = 0


def print_embed_stats(prefix: str = "") -> None:
    est = EMBED_STATS["chars"] / 4.0
    api = EMBED_STATS["tokens_api"]
    print(
        f"{prefix}embed_stats requests={EMBED_STATS['requests']} "
        f"texts={EMBED_STATS['texts']} chars={EMBED_STATS['chars']} "
        f"tokens_est(chars/4)={est:.0f} tokens_api={api or '—'}",
        flush=True,
    )


def modo_embed_fila(row: dict, mode: str) -> str:
    if mode in ("header", "body"):
        return mode
    return "body" if (row.get("fuente") or "") == "pdf" else "header"


def texto_para_embed(row: dict, mode: str) -> str:
    t = (row.get("texto") or "")[:MAX_CHARS_GEMINI]
    if modo_embed_fila(row, mode) == "body":
        t = cuerpo_chunk(t)[:MAX_CHARS_GEMINI]
    return t


def reset_embedding_v2(supa, ids: list[int], fuente: str) -> int:
    """Pone embedding_v2=NULL solo en la muestra (requiere ids+fuente)."""
    if not ids or not fuente:
        raise SystemExit("ERROR: --reset-v2 exige --ids y --fuente (no masivo)")
    n = 0
    for i in range(0, len(ids), 80):
        lote = ids[i:i + 80]
        res = (
            supa.table("chunks_tdr")
            .update({"embedding_v2": None})
            .in_("contrato_id", lote)
            .eq("fuente", fuente)
            .execute()
        )
        n += len(res.data or [])
    return n


def vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


def l2_normalize(vec: list[float]) -> list[float]:
    """gemini-embedding-001 exige renormalizar si dim != 3072."""
    s = math.sqrt(sum(x * x for x in vec))
    if s <= 0:
        return vec
    return [x / s for x in vec]


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


def paginar_ids_vigentes(supa) -> list[int]:
    ids: list[int] = []
    offset = 0
    while True:
        res = (
            supa.table("contratos")
            .select("id")
            .eq("estado", "Vigente")
            .order("id")
            .range(offset, offset + PAGE - 1)
            .execute()
        )
        batch = res.data or []
        ids.extend(int(r["id"]) for r in batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return ids


def chunks_sin_embedding_v2(
    supa,
    vigente_ids: list[int],
    limit: int,
    fuente: str | None = None,
) -> list[dict]:
    """Solo chunks de vigentes con embedding_v2 NULL. No re-embebe filas ya llenas."""
    out: list[dict] = []
    for i in range(0, len(vigente_ids), 80):
        lote_ids = vigente_ids[i:i + 80]
        offset = 0
        while True:
            take = PAGE if not limit else min(PAGE, limit - len(out))
            if take <= 0:
                return out
            q = (
                supa.table("chunks_tdr")
                .select("id, contrato_id, chunk_index, tipo, texto, fuente")
                .in_("contrato_id", lote_ids)
                .is_("embedding_v2", "null")
            )
            if fuente:
                q = q.eq("fuente", fuente)
            res = q.order("id").range(offset, offset + take - 1).execute()
            batch = res.data or []
            out.extend(batch)
            if len(batch) < take:
                break
            offset += take
            if limit and len(out) >= limit:
                return out[:limit]
    return out[:limit] if limit else out


def embed_lote_gemini(
    client: httpx.Client,
    texts: list[str],
    fail_fast: bool = False,
) -> list[list[float]]:
    payload = {
        "requests": [
            {
                "model": f"models/{GEMINI_EMBED_MODEL}",
                "content": {"parts": [{"text": t}]},
                "taskType": "RETRIEVAL_DOCUMENT",
                "outputDimensionality": GEMINI_DIM,
            }
            for t in texts
        ]
    }
    waits = [0.0] if fail_fast else [0.0] + list(GEMINI_BACKOFF)
    last_err: Exception | None = None
    for attempt, wait in enumerate(waits):
        if wait:
            print(f"    [gemini backoff {wait:.0f}s attempt={attempt}]", flush=True)
            time.sleep(wait)
        try:
            r = client.post(
                GEMINI_EMBED_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": GEMINI_API_KEY,
                },
                json=payload,
                timeout=120.0,
            )
            if r.status_code == 429:
                msg = f"429 {r.text[:200]}"
                if fail_fast:
                    raise QuotaExceeded(msg)
                last_err = RuntimeError(msg)
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        extra = min(float(retry_after), 120.0)
                        print(f"    [429 Retry-After {extra:.0f}s]", flush=True)
                        time.sleep(extra)
                    except ValueError:
                        pass
                continue
            r.raise_for_status()
            body = r.json()
            raw = body.get("embeddings")
            if not isinstance(raw, list) or len(raw) != len(texts):
                raise RuntimeError(
                    f"gemini respuesta inesperada keys={list(body)[:8]} "
                    f"n={len(raw) if isinstance(raw, list) else None}"
                )
            EMBED_STATS["requests"] += 1
            EMBED_STATS["texts"] += len(texts)
            EMBED_STATS["chars"] += sum(len(t) for t in texts)
            um = body.get("usageMetadata") or {}
            tok = um.get("totalTokenCount") or um.get("promptTokenCount") or 0
            try:
                EMBED_STATS["tokens_api"] += int(tok or 0)
            except (TypeError, ValueError):
                pass
            if EMBED_STATS["requests"] == 1:
                print(f"  gemini keys={list(body)[:12]} usage={um or '—'}", flush=True)
            out: list[list[float]] = []
            for item in raw:
                vals = item.get("values") if isinstance(item, dict) else None
                if not isinstance(vals, list) or not vals:
                    raise RuntimeError("gemini embedding vacío")
                if len(vals) > GEMINI_DIM:
                    vals = vals[:GEMINI_DIM]
                if len(vals) != GEMINI_DIM:
                    raise RuntimeError(f"dimensión {len(vals)} != {GEMINI_DIM}")
                out.append(l2_normalize([float(x) for x in vals]))
            return out
        except QuotaExceeded:
            raise
        except httpx.HTTPStatusError as e:
            last_err = e
            code = e.response.status_code if e.response is not None else 0
            if fail_fast and code == 429:
                raise QuotaExceeded(str(e)) from e
            if e.response is not None and e.response.status_code in (429, 500, 503):
                if fail_fast:
                    raise
                print(f"    [retry {attempt}] HTTP {e.response.status_code}", flush=True)
                continue
            raise
        except Exception as e:
            last_err = e
            print(f"    [retry {attempt}] {e}", flush=True)
            if fail_fast:
                raise
    raise RuntimeError(f"embed_lote_gemini falló: {last_err}")


def cobertura_vigentes(supa) -> dict[str, int]:
    ids = paginar_ids_vigentes(supa)
    total = 0
    con_v2 = 0
    con_v1 = 0
    for i in range(0, len(ids), 80):
        lote = ids[i:i + 80]
        t = (
            supa.table("chunks_tdr")
            .select("id", count="exact")
            .in_("contrato_id", lote)
            .limit(1)
            .execute()
        )
        total += t.count or 0
        v2 = (
            supa.table("chunks_tdr")
            .select("id", count="exact")
            .in_("contrato_id", lote)
            .not_.is_("embedding_v2", "null")
            .limit(1)
            .execute()
        )
        con_v2 += v2.count or 0
        v1 = (
            supa.table("chunks_tdr")
            .select("id", count="exact")
            .in_("contrato_id", lote)
            .not_.is_("embedding", "null")
            .limit(1)
            .execute()
        )
        con_v1 += v1.count or 0
    return {
        "vigentes": len(ids),
        "chunks_vigentes": total,
        "chunks_v2": con_v2,
        "chunks_v1_768": con_v1,
        "chunks_v2_null": total - con_v2,
    }


def print_cobertura(cov: dict[str, int]) -> None:
    tot = cov["chunks_vigentes"]
    pct = (100.0 * cov["chunks_v2"] / tot) if tot else 0.0
    pct768 = (100.0 * cov["chunks_v1_768"] / tot) if tot else 0.0
    print("=" * 60, flush=True)
    print("COBERTURA chunks de VIGENTES", flush=True)
    print(f"  contratos vigentes     : {cov['vigentes']:,}", flush=True)
    print(f"  chunks vigentes        : {tot:,}", flush=True)
    print(f"  embedding_v2 NOT NULL  : {cov['chunks_v2']:,}  ({pct:.1f}%)", flush=True)
    print(f"  embedding_v2 NULL      : {cov['chunks_v2_null']:,}", flush=True)
    print(f"  embedding(768) NOT NULL: {cov['chunks_v1_768']:,}  ({pct768:.1f}%)", flush=True)
    print("=" * 60, flush=True)


def run_bge(supa, limit: int) -> None:
    print("=" * 60, flush=True)
    print("FASE 4 — Embeddings bge-base-en-v1.5 → embedding(768)", flush=True)
    print(f"  endpoint: {EMBED_URL}", flush=True)
    print("=" * 60, flush=True)

    pendientes = chunks_sin_embedding(supa, limit)
    total = len(pendientes)
    print(f"Chunks sin embedding(768): {total:,}", flush=True)
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


def run_gemini(
    supa,
    limit: int,
    fuente: str | None = None,
    ids: list[int] | None = None,
    batch: int | None = None,
    embed_mode: str = "auto",
    delay: float | None = None,
    fail_fast: bool = False,
) -> dict:
    if not GEMINI_API_KEY:
        raise SystemExit("ERROR: GEMINI_API_KEY no encontrado (env / .env / GitHub secret)")

    lote_n = batch if batch and batch > 0 else BATCH_GEMINI
    pause = delay if delay is not None and delay >= 0 else (
        (2.0 if fail_fast else 8.0) if lote_n <= 2 else DELAY_GEMINI_S
    )
    print("=" * 60, flush=True)
    print("Embeddings gemini-embedding-001 @1536 → embedding_v2", flush=True)
    print("  taskType=RETRIEVAL_DOCUMENT  WHERE embedding_v2 IS NULL", flush=True)
    print(
        f"  fuente={fuente or '(todas)'}  ids={ids or '(vigentes)'}  "
        f"batch={lote_n}  embed_mode={embed_mode}  delay={pause:.1f}s  "
        f"fail_fast={fail_fast}",
        flush=True,
    )
    print("=" * 60, flush=True)

    vigente_ids = ids if ids else paginar_ids_vigentes(supa)
    print(f"  contratos: {len(vigente_ids):,}", flush=True)
    pendientes = chunks_sin_embedding_v2(supa, vigente_ids, limit, fuente=fuente)
    total = len(pendientes)
    print(f"  chunks vigentes sin embedding_v2: {total:,}", flush=True)
    if total == 0:
        print("Nada que hacer (idempotente).", flush=True)
        if fuente or ids:
            print("  (muestra: 0 pendientes)", flush=True)
        else:
            print_cobertura(cobertura_vigentes(supa))
        return {"ok": 0, "err": 0, "total": 0, "pendientes": 0}

    t0 = time.time()
    ok = 0
    errores = 0

    with httpx.Client() as http:
        for i in range(0, total, lote_n):
            lote = pendientes[i:i + lote_n]
            texts = [texto_para_embed(row, embed_mode) for row in lote]
            if i == 0 and texts:
                preview = texts[0][:80].replace("\n", " | ")
                print(f"  preview embed[0]={preview!r}", flush=True)
            try:
                embs = embed_lote_gemini(http, texts, fail_fast=fail_fast)
                for row, vec in zip(lote, embs):
                    (
                        supa.table("chunks_tdr")
                        .update({"embedding_v2": vec_literal(vec)})
                        .eq("id", row["id"])
                        .execute()
                    )
                ok += len(lote)
            except QuotaExceeded as e:
                errores += len(lote)
                pending = total - ok
                print(
                    f"  STOP 429  ok={ok}/{total}  lote={i}-{i+len(lote)}  "
                    f"sin backoff. {e}",
                    flush=True,
                )
                raise QuotaExceeded(f"ok={ok} pendientes={pending}: {e}") from e
            except Exception as e:
                errores += len(lote)
                print(f"  [error] lote {i}-{i+len(lote)}: {e}", flush=True)
                if fail_fast:
                    raise

            done = min(i + lote_n, total)
            if done % max(lote_n, 1) == 0 or done == total:
                elapsed = time.time() - t0
                rate = ok / elapsed if elapsed else 0
                print(
                    f"  [{done}/{total}] ok={ok} err={errores} "
                    f"{elapsed:.0f}s  {rate:.1f}/s",
                    flush=True,
                )
            time.sleep(pause)

    elapsed = time.time() - t0
    print(f"\nGemini v2 completado en {elapsed:.0f}s  ok={ok:,} err={errores:,}", flush=True)
    print_embed_stats("  ")
    if fuente or ids:
        n_pdf = (
            supa.table("chunks_tdr")
            .select("id", count="exact")
            .in_("contrato_id", vigente_ids)
        )
        if fuente:
            n_pdf = n_pdf.eq("fuente", fuente)
        n_pdf = n_pdf.not_.is_("embedding_v2", "null").limit(1).execute()
        print(f"  embedding_v2 NOT NULL en muestra: {n_pdf.count}", flush=True)
    else:
        print_cobertura(cobertura_vigentes(supa))
    return {"ok": ok, "err": errores, "total": total, "pendientes": total - ok}


def auth_check_gemini() -> int:
    """Llama a Gemini y reporta solo el HTTP. No imprime la key ni el body."""
    if not GEMINI_API_KEY:
        print("gemini_auth HTTP=missing auth_ok=false", flush=True)
        return 2
    r = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_EMBED_MODEL}:embedContent",
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
        },
        json={
            "model": f"models/{GEMINI_EMBED_MODEL}",
            "content": {"parts": [{"text": "ok"}]},
            "taskType": "RETRIEVAL_DOCUMENT",
            "outputDimensionality": GEMINI_DIM,
        },
        timeout=30.0,
    )
    code = r.status_code
    auth_fail = code in (401, 403)
    ok = (not auth_fail) and code < 400
    print(f"gemini_auth HTTP={code} auth_fail={auth_fail} auth_ok={ok}", flush=True)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = todos")
    ap.add_argument(
        "--backend",
        choices=["bge", "gemini"],
        default="bge",
        help="bge = embedding(768) cron v1; gemini = embedding_v2(1536) vigentes",
    )
    ap.add_argument(
        "--cobertura",
        action="store_true",
        help="Solo reporta cobertura v2 de vigentes; no embebe",
    )
    ap.add_argument(
        "--fuente",
        default="",
        help="Filtra chunks por fuente (api|pdf). Vacio = todas.",
    )
    ap.add_argument(
        "--ids",
        default="",
        help="Ids de contrato separados por coma (no re-embebe el resto)",
    )
    ap.add_argument(
        "--batch",
        type=int,
        default=0,
        help="Tamano de lote Gemini (0 = default 16)",
    )
    ap.add_argument(
        "--embed-mode",
        choices=["auto", "header", "body"],
        default="auto",
        help="auto: pdf=cuerpo sin header, api=texto completo. "
             "header/body fuerzan el modo (A/B del header PDF).",
    )
    ap.add_argument(
        "--delay",
        type=float,
        default=-1,
        help="Pausa entre lotes Gemini en segundos (-1 = auto: 8s si batch<=2)",
    )
    ap.add_argument(
        "--reset-v2",
        action="store_true",
        help="Pone embedding_v2=NULL en --ids + --fuente (no embebe). Exige ambos.",
    )
    ap.add_argument(
        "--fail-fast",
        action="store_true",
        help="Ante 429 PARA sin backoff (validacion de muestra).",
    )
    ap.add_argument(
        "--auth-check",
        action="store_true",
        help="Ping Gemini (embedContent) y sale. No toca la BD ni imprime la key.",
    )
    args = ap.parse_args()

    if args.auth_check:
        raise SystemExit(auth_check_gemini())

    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SystemExit("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY no encontrados")

    supa = create_client(SUPABASE_URL, SUPABASE_KEY)
    if args.cobertura:
        print_cobertura(cobertura_vigentes(supa))
        return

    ids = [int(x) for x in args.ids.replace(" ", "").split(",") if x] if args.ids else None
    if args.reset_v2:
        n = reset_embedding_v2(supa, ids or [], args.fuente)
        print(f"reset embedding_v2: {n} filas (fuente={args.fuente} ids={ids})", flush=True)
        return

    if args.backend == "gemini":
        run_gemini(
            supa,
            args.limit,
            fuente=(args.fuente or None),
            ids=ids,
            batch=(args.batch or None),
            embed_mode=args.embed_mode,
            delay=(None if args.delay < 0 else args.delay),
            fail_fast=args.fail_fast,
        )
    else:
        run_bge(supa, args.limit)


if __name__ == "__main__":
    main()
