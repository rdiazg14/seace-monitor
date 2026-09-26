"""Orquestacion de embeddings sin configuracion global del entrypoint."""
from __future__ import annotations

import time

import httpx

from seace_monitor.embeddings.gemini_provider import (
    GEMINI_EMBED_MODEL,
    QuotaExceeded,
    solicitar_embeddings_gemini,
)
from seace_monitor.embeddings.preparation import (
    EMBED_STATS,
    print_embed_stats,
    texto_para_embed,
)
from seace_monitor.embeddings.repository import (
    chunks_sin_embedding_v2,
    chunks_sin_v2_por_fuente,
    contar_embeddings_v2,
    cobertura_vigentes,
    guardar_embeddings_v2,
    paginar_ids_vigentes,
)
from seace_monitor.gemini import EMBED_USD_PER_M
from seace_monitor.logging import PASO_EMBEDDING, registrar_evento, registrar_run

BATCH_GEMINI = 16
DELAY_GEMINI_S = 0.4


def print_cobertura(cov: dict[str, int]) -> None:
    tot = cov["chunks_vigentes"]
    pct = (100.0 * cov["chunks_v2"] / tot) if tot else 0.0
    print("=" * 60, flush=True)
    print("COBERTURA chunks de VIGENTES", flush=True)
    print(f"  contratos vigentes     : {cov['vigentes']:,}", flush=True)
    print(f"  chunks vigentes        : {tot:,}", flush=True)
    print(f"  embedding_v2 NOT NULL  : {cov['chunks_v2']:,}  ({pct:.1f}%)", flush=True)
    print(f"  embedding_v2 NULL      : {cov['chunks_v2_null']:,}", flush=True)
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
    api_key: str = "",
    http_client_factory=httpx.Client,
    sleep=time.sleep,
) -> dict:
    if not api_key:
        raise SystemExit("ERROR: GEMINI_API_KEY no encontrado (env / .env / GitHub secret)")

    lote_n = batch if batch and batch > 0 else BATCH_GEMINI
    pause = delay if delay is not None and delay >= 0 else (
        (2.0 if fail_fast else 8.0) if lote_n <= 2 else DELAY_GEMINI_S
    )
    print("=" * 60, flush=True)
    print("Embeddings gemini-embedding-001 @1536 -> embedding_v2", flush=True)
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
    if fuente:
        pendientes = chunks_sin_v2_por_fuente(supa, fuente, limit)
        if ids:
            idset = set(ids)
            pendientes = [p for p in pendientes if int(p["contrato_id"]) in idset]
    else:
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
    por_contrato: dict[int, dict] = {}

    with http_client_factory() as http:
        for i in range(0, total, lote_n):
            lote = pendientes[i:i + lote_n]
            texts = [texto_para_embed(row, embed_mode) for row in lote]
            if i == 0 and texts:
                preview = texts[0][:80].replace("\n", " | ")
                print(f"  preview embed[0]={preview!r}", flush=True)
            try:
                embs = solicitar_embeddings_gemini(
                    http, texts, api_key, fail_fast=fail_fast
                )
                # Un solo upsert por lote (no N requests). Las filas ya existen,
                # así que on_conflict=id solo actualiza embedding_v2.
                guardar_embeddings_v2(supa, lote, embs)
                ok += len(lote)
                for row, t in zip(lote, texts):
                    cid = int(row["contrato_id"])
                    acc = por_contrato.setdefault(cid, {"chunks": 0, "chars": 0})
                    acc["chunks"] += 1
                    acc["chars"] += len(t)
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
            sleep(pause)

    elapsed = time.time() - t0
    print(f"\nGemini v2 completado en {elapsed:.0f}s  ok={ok:,} err={errores:,}", flush=True)
    print_embed_stats("  ")
    if fuente or ids:
        n_embedded = contar_embeddings_v2(supa, vigente_ids, fuente)
        print(f"  embedding_v2 NOT NULL en muestra: {n_embedded}", flush=True)
    else:
        print_cobertura(cobertura_vigentes(supa))
    # Traza unificada de consumo: una fila por corrida de embeddings.
    tok = EMBED_STATS["tokens_api"]
    if tok > 0:
        try:
            supa.table("uso_ia").insert({
                "componente": "embedding",
                "modelo": GEMINI_EMBED_MODEL,
                "tokens_prompt": tok,
                "tokens_total": tok,
                "costo_usd": round(tok / 1_000_000.0 * EMBED_USD_PER_M, 8),
                "cache_hit": False,
                "detalle": {
                    "texts": EMBED_STATS["texts"],
                    "requests": EMBED_STATS["requests"],
                    "n_chunks": ok,
                    "n_contratos": len(por_contrato),
                    "fuente": fuente,
                    "ids": ids or None,
                },
            }).execute()
        except Exception as e:
            print(f"  [warn] log_uso_ia embeddings: {e}", flush=True)

    # Seguimiento por contrato: prorrateo del costo de embedding por chars.
    for cid, acc in por_contrato.items():
        chars = acc["chars"]
        tokens_est = chars // 4
        costo = tokens_est / 1_000_000.0 * EMBED_USD_PER_M
        registrar_evento(
            supa,
            cid,
            "embedded",
            n_chunks_pdf=acc["chunks"] if fuente == "pdf" else None,
            n_chunks_api=acc["chunks"] if fuente == "api" else None,
            tokens_est=tokens_est,
            costo_usd=costo,
            detalle={"chars": chars, "fuente": fuente, "modelo": GEMINI_EMBED_MODEL},
        )

    registrar_run(
        supa,
        PASO_EMBEDDING,
        {
            "ok": ok,
            "errores": errores,
            "total": total,
            "contratos": len(por_contrato),
            "tokens_api": tok,
            "costo_usd": round(tok / 1_000_000.0 * EMBED_USD_PER_M, 8),
            "fuente": fuente,
            "ids": ids or None,
        },
    )
    return {"ok": ok, "err": errores, "total": total, "pendientes": total - ok}
