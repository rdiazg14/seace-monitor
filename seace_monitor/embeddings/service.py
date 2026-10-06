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
    chunks_sin_embedding,
    chunks_sin_por_fuente,
    contar_embeddings,
    cobertura_columna,
    guardar_embeddings,
    paginar_ids_vigentes,
)
from seace_monitor.gemini import EMBED_USD_PER_M
from seace_monitor.logging import PASO_EMBEDDING, registrar_evento, registrar_run

BATCH_GEMINI = 16
DELAY_GEMINI_S = 0.4

# Espacio vectorial que alimenta cada columna de chunks_tdr (IA-007).
ESPACIO_POR_COLUMNA = {
    "embedding_v2": "gemini-emb001-1536",
    "embedding_v3": "qwen-tev4-1536",
}

# Inverso (FIX-012): el espacio_vectorial de la config dinámica determina la
# columna destino; un espacio jamás escribe en la columna de otro.
COLUMNA_POR_ESPACIO = {espacio: col for col, espacio in ESPACIO_POR_COLUMNA.items()}


def print_cobertura(cov: dict) -> None:
    # cobertura_columna usa claves genéricas; cobertura_vigentes conserva las
    # históricas chunks_v2* — ambas se imprimen igual.
    col = str(cov.get("col") or "embedding_v2")
    tot = int(cov["chunks_vigentes"])
    con = int(cov.get("chunks_col", cov.get("chunks_v2", 0)))
    nul = int(cov.get("chunks_col_null", cov.get("chunks_v2_null", 0)))
    pct = (100.0 * con / tot) if tot else 0.0
    print("=" * 60, flush=True)
    print(f"COBERTURA chunks de VIGENTES ({col})", flush=True)
    print(f"  contratos vigentes     : {cov['vigentes']:,}", flush=True)
    print(f"  chunks vigentes        : {tot:,}", flush=True)
    print(f"  {col} NOT NULL : {con:,}  ({pct:.1f}%)", flush=True)
    print(f"  {col} NULL     : {nul:,}", flush=True)
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
    solicitar=None,
    modelo: str | None = None,
    precio_in: float | None = None,
    version_config: int = 0,
    verificar_config=None,
    columna: str = "embedding_v2",
) -> dict:
    if not api_key:
        raise SystemExit("ERROR: GEMINI_API_KEY no encontrado (env / .env / GitHub secret)")
    espacio = ESPACIO_POR_COLUMNA.get(columna)
    if espacio is None:
        raise ValueError(f"columna de embedding no soportada: {columna!r}")
    solicitar = solicitar or solicitar_embeddings_gemini
    modelo = modelo or GEMINI_EMBED_MODEL
    precio_in = EMBED_USD_PER_M if precio_in is None else precio_in

    lote_n = batch if batch and batch > 0 else BATCH_GEMINI
    pause = delay if delay is not None and delay >= 0 else (
        (2.0 if fail_fast else 8.0) if lote_n <= 2 else DELAY_GEMINI_S
    )
    print("=" * 60, flush=True)
    print(f"Embeddings {modelo} -> {columna}", flush=True)
    print(f"  WHERE {columna} IS NULL", flush=True)
    print(
        f"  fuente={fuente or '(todas)'}  ids={ids or '(vigentes)'}  "
        f"batch={lote_n}  embed_mode={embed_mode}  delay={pause:.1f}s  "
        f"fail_fast={fail_fast}  version_config={version_config or '-'}",
        flush=True,
    )
    print("=" * 60, flush=True)

    vigente_ids = ids if ids else paginar_ids_vigentes(supa)
    print(f"  contratos: {len(vigente_ids):,}", flush=True)
    if fuente:
        pendientes = chunks_sin_por_fuente(supa, columna, fuente, limit)
        if ids:
            idset = set(ids)
            pendientes = [p for p in pendientes if int(p["contrato_id"]) in idset]
    else:
        pendientes = chunks_sin_embedding(supa, columna, vigente_ids, limit, fuente=fuente)
    total = len(pendientes)
    print(f"  chunks vigentes sin {columna}: {total:,}", flush=True)
    if total == 0:
        print("Nada que hacer (idempotente).", flush=True)
        if fuente or ids:
            print("  (muestra: 0 pendientes)", flush=True)
        else:
            print_cobertura(cobertura_columna(supa, columna))
        return {"ok": 0, "err": 0, "total": 0, "pendientes": 0}

    t0 = time.time()
    ok = 0
    errores = 0
    por_contrato: dict[int, dict] = {}

    with http_client_factory() as http:
        for i in range(0, total, lote_n):
            if verificar_config is not None:
                verificar_config()
            lote = pendientes[i:i + lote_n]
            texts = [texto_para_embed(row, embed_mode) for row in lote]
            if i == 0 and texts:
                preview = texts[0][:80].replace("\n", " | ")
                print(f"  preview embed[0]={preview!r}", flush=True)
            try:
                embs = solicitar(
                    http, texts, api_key, fail_fast=fail_fast
                )
                if verificar_config is not None:
                    verificar_config()
                # Un solo upsert por lote (no N requests). Las filas ya existen,
                # así que on_conflict=id solo actualiza la columna del espacio.
                guardar_embeddings(supa, columna, lote, embs)
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
    print(
        f"\n{modelo} {columna} completado en {elapsed:.0f}s  "
        f"ok={ok:,} err={errores:,}",
        flush=True,
    )
    print_embed_stats("  ")
    if fuente or ids:
        n_embedded = contar_embeddings(supa, columna, vigente_ids, fuente)
        print(f"  {columna} NOT NULL en muestra: {n_embedded}", flush=True)
    else:
        print_cobertura(cobertura_columna(supa, columna))
    # Traza unificada de consumo: una fila por corrida de embeddings.
    tok = EMBED_STATS["tokens_api"]
    if tok > 0:
        try:
            supa.table("uso_ia").insert({
                "componente": "embedding",
                "modelo": modelo,
                "tokens_prompt": tok,
                "tokens_total": tok,
                "costo_usd": round(tok / 1_000_000.0 * precio_in, 8),
                "cache_hit": False,
                "detalle": {
                    "texts": EMBED_STATS["texts"],
                    "requests": EMBED_STATS["requests"],
                    "n_chunks": ok,
                    "n_contratos": len(por_contrato),
                    "fuente": fuente,
                    "ids": ids or None,
                    "version_config": version_config or None,
                    "columna": columna,
                    "espacio": espacio,
                },
            }).execute()
        except Exception as e:
            print(f"  [warn] log_uso_ia embeddings: {e}", flush=True)

    # Seguimiento por contrato: prorrateo del costo de embedding por chars.
    for cid, acc in por_contrato.items():
        chars = acc["chars"]
        tokens_est = chars // 4
        costo = tokens_est / 1_000_000.0 * precio_in
        registrar_evento(
            supa,
            cid,
            "embedded",
            n_chunks_pdf=acc["chunks"] if fuente == "pdf" else None,
            n_chunks_api=acc["chunks"] if fuente == "api" else None,
            tokens_est=tokens_est,
            costo_usd=costo,
            detalle={
                "chars": chars,
                "fuente": fuente,
                "modelo": modelo,
                "espacio": espacio,
            },
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
            "costo_usd": round(tok / 1_000_000.0 * precio_in, 8),
            "fuente": fuente,
            "ids": ids or None,
            "modelo": modelo,
            "version_config": version_config or None,
            "columna": columna,
            "espacio": espacio,
        },
    )
    return {"ok": ok, "err": errores, "total": total, "pendientes": total - ok}
