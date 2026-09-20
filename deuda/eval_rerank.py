#!/usr/bin/env python3
"""
G4b — eval post-rerank (bge-reranker-base) vs pre-rerank (RRF).

Mide si el reranker reordena el top-20 (fusión vector+FTS+RRF) de forma que
los primeros K fragmentos tengan MÁS relevantes que el orden RRF original.

Replica fielmente retrieveContextV2 del Worker:
  vector (buscar_tdr_v2, 20 chunks) + FTS (buscar_contratos, 20)
  -> RRF a nivel contrato (top 20) -> candidates (<=2 chunks por contrato)
  -> top20 -> reranker (env.AI.run bge-reranker-base) -> top K.

Usa el endpoint interno /rerank del Worker (protegido por FUNNEL_TOKEN),
que corre el MISMO binding AI de producción.

Uso:
  python eval_rerank.py
  python eval_rerank.py --topk 10
"""
from __future__ import annotations
import _bootstrap  # noqa: F401

import argparse
import json
import os
import time
from pathlib import Path

import httpx
from supabase import create_client

import eval_retrieval as ev

# eval_retrieval ya carga .env a nivel de módulo; aquí solo exponemos lo extra.
RERANK_URL = os.environ.get(
    "RERANK_URL",
    "https://seace-ai-proxy.rdiazg14.workers.dev/rerank",
)
FUNNEL_TOKEN = os.environ.get("FUNNEL_TOKEN", "")

if not ev.SUPABASE_URL or not ev.SUPABASE_KEY:
    raise SystemExit("ERROR: .env sin SUPABASE_URL / SUPABASE_SERVICE_KEY")
if not ev.GEMINI_API_KEY:
    raise SystemExit("ERROR: GEMINI_API_KEY requerido")
if not FUNNEL_TOKEN:
    raise SystemExit("ERROR: FUNNEL_TOKEN requerido para /rerank")

supa = create_client(ev.SUPABASE_URL, ev.SUPABASE_KEY)
OUT = Path(__file__).parent / "data" / "eval_rerank.json"


def fetch_meta(ids: list[int]) -> dict[int, dict]:
    """contratos (sin categoria_it, dropeada en fase 6) + clasificacion_contrato."""
    out: dict[int, dict] = {}
    uniq = list(dict.fromkeys(ids))
    for i in range(0, len(uniq), 80):
        lote = uniq[i:i + 80]
        res = (
            supa.table("contratos")
            .select("id,estado,descripcion,descripcion_contrato")
            .in_("id", lote)
            .execute()
        )
        for row in res.data or []:
            out[int(row["id"])] = row
        res2 = (
            supa.table("clasificacion_contrato")
            .select("contrato_id,categoria_it,relevancia_ia")
            .in_("contrato_id", lote)
            .execute()
        )
        for row in res2.data or []:
            cid = int(row["contrato_id"])
            if cid in out:
                out[cid]["categoria_it"] = row.get("categoria_it")
                out[cid]["relevancia_ia"] = row.get("relevancia_ia")
    return out


def rerank(http: httpx.Client, query: str, contexts: list[str], top_k: int) -> list[int]:
    """Devuelve los índices originales en el orden del reranker. [] si falla."""
    payload = {"query": query, "contexts": contexts, "top_k": top_k}
    headers = {"Authorization": f"Bearer {FUNNEL_TOKEN}"}
    last = ""
    for attempt in range(3):
        try:
            r = http.post(RERANK_URL, json=payload, headers=headers, timeout=60.0)
            if r.status_code == 200:
                raw = (r.json() or {}).get("result") or {}
                rows = raw.get("response") or raw.get("result") or raw.get("data") or []
                idx = []
                for row in rows:
                    i = row.get("id") if isinstance(row.get("id"), int) else row.get("index")
                    if isinstance(i, int):
                        idx.append(i)
                return idx
            last = f"http {r.status_code}: {r.text[:200]}"
        except Exception as e:
            last = f"exc {e}"
        time.sleep(1.0 * (attempt + 1))
    print(f"    [rerank-fail] {query[:30]!r} -> {last}", file=__import__('sys').stderr, flush=True)
    return []


def build_top20(q: dict, termino: str, vec: list[float]) -> list[dict]:
    vec_hits = ev.buscar_v2_vector(supa, vec, 20, 0.20)
    fts_ids = ev.gold_fts(supa, termino or q["must"][0], n=20)

    vec_by_cid: dict[int, dict] = {}
    vec_order: list[int] = []
    for h in vec_hits:
        cid = int(h["contrato_id"])
        if cid not in vec_by_cid:
            vec_by_cid[cid] = h
            vec_order.append(cid)

    scores: dict[int, float] = {}
    best: dict[int, dict] = {}
    for rank, cid in enumerate(vec_order, 1):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (ev.RRF_K + rank)
        best[cid] = vec_by_cid[cid]
    for rank, cid in enumerate(fts_ids, 1):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (ev.RRF_K + rank)
        if cid not in best:
            best[cid] = {
                "contrato_id": cid, "chunk_index": -1, "tipo": "FTS",
                "texto": "", "similarity": 0.0, "fuente": "fts",
            }

    fused = sorted(scores, key=lambda c: -scores[c])[:20]
    candidates: list[dict] = []
    for cid in fused:
        chunks_cid = [h for h in vec_hits if int(h["contrato_id"]) == cid]
        if chunks_cid:
            candidates.extend(chunks_cid[:2])
        else:
            candidates.append(best[cid])
    return candidates[:20]


def judge(items: list[dict], q: dict, meta: dict[int, dict]) -> list[bool]:
    out = []
    for h in items:
        cid = int(h["contrato_id"])
        c = meta.get(cid) or {}
        blob = " ".join([
            h.get("texto") or "",
            c.get("descripcion") or "",
            c.get("descripcion_contrato") or "",
            c.get("categoria_it") or "",
        ])
        out.append(ev.es_relevante(blob, q))
    return out


def p_at(rel: list[bool], k: int) -> float:
    if not rel:
        return 0.0
    n = min(k, len(rel))
    return sum(1 for x in rel[:n] if x) / n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topk", type=int, default=10,
                    help="Cantidad devuelta por el reranker (default 10).")
    args = ap.parse_args()
    topk = args.topk

    print("=" * 74, flush=True)
    print("G4b eval post-rerank (bge-reranker-base) vs pre-rerank (RRF)", flush=True)
    print(f"  topk={topk}  n_queries={len(ev.QUERIES)}  rerank_url={RERANK_URL}", flush=True)
    print("=" * 74, flush=True)

    terminos = [ev.extraer_termino(q["query"]) or q["query"] for q in ev.QUERIES]

    t_emb0 = time.time()
    vecs: list[list[float]] = []
    with httpx.Client() as http:
        for t in terminos:
            vecs.append(ev.embed_query_gemini(http, t))
            time.sleep(0.15)
    emb_s = time.time() - t_emb0
    print(f"  embeddings: {len(vecs)} en {emb_s:.1f}s", flush=True)

    filas = []
    t0 = time.time()
    with httpx.Client() as http:
        for q, termino, vec in zip(ev.QUERIES, terminos, vecs):
            top20 = build_top20(q, termino, vec)
            ids = [int(h["contrato_id"]) for h in top20]
            meta = fetch_meta(ids)

            pre_rel = judge(top20, q, meta)  # orden RRF original

            contexts = [(h.get("texto") or "") for h in top20]
            rr_idx = rerank(http, termino or q["query"], contexts, topk)
            if rr_idx:
                post_items = [top20[i] for i in rr_idx if i < len(top20)]
                # relleno con lo que falte (no duplicar)
                seen = {i for i in rr_idx if i < len(top20)}
                for i in range(len(top20)):
                    if i not in seen:
                        post_items.append(top20[i])
                post_rel = judge(post_items, q, meta)
            else:
                post_rel = pre_rel  # reranker falló: sin reorden

            filas.append({
                "id": q["id"],
                "query": q["query"],
                "n_candidates": len(top20),
                "pre_P@5": round(p_at(pre_rel, 5), 3),
                "pre_P@10": round(p_at(pre_rel, 10), 3),
                "pre_rel_n": sum(pre_rel),
                "post_P@5": round(p_at(post_rel, 5), 3),
                "post_P@10": round(p_at(post_rel, 10), 3),
                "post_rel_n": sum(post_rel),
                "rerank_ok": bool(rr_idx),
            })
            d = (p_at(post_rel, 5) - p_at(pre_rel, 5), p_at(post_rel, 10) - p_at(pre_rel, 10))
            print(
                f"  {q['id']:<22} cand={len(top20):2}  "
                f"pre P@5={p_at(pre_rel,5):.2f} P@10={p_at(pre_rel,10):.2f}  "
                f"post P@5={p_at(post_rel,5):.2f} P@10={p_at(post_rel,10):.2f}  "
                f"d5={d[0]:+.2f} d10={d[1]:+.2f}",
                flush=True,
            )

    n = len(filas)
    pre_p5 = sum(f["pre_P@5"] for f in filas) / n
    pre_p10 = sum(f["pre_P@10"] for f in filas) / n
    post_p5 = sum(f["post_P@5"] for f in filas) / n
    post_p10 = sum(f["post_P@10"] for f in filas) / n
    rerank_ok = sum(1 for f in filas if f["rerank_ok"])

    resumen = {
        "modelo": "@cf/baai/bge-reranker-base",
        "topk": topk,
        "n_queries": n,
        "macro_P@5_pre": round(pre_p5, 3),
        "macro_P@10_pre": round(pre_p10, 3),
        "macro_P@5_post": round(post_p5, 3),
        "macro_P@10_post": round(post_p10, 3),
        "delta_P@5": round(post_p5 - pre_p5, 3),
        "delta_P@10": round(post_p10 - pre_p10, 3),
        "rerank_ok": rerank_ok,
        "elapsed_s": round(time.time() - t0 + emb_s, 1),
    }

    print("\n" + "=" * 74, flush=True)
    print("EVAL post-rerank (bge-reranker-base)", flush=True)
    print(f"  macro P@5  : pre={pre_p5:.3f} -> post={post_p5:.3f}  (d {post_p5 - pre_p5:+.3f})", flush=True)
    print(f"  macro P@10 : pre={pre_p10:.3f} -> post={post_p10:.3f}  (d {post_p10 - pre_p10:+.3f})", flush=True)
    print(f"  rerank ok  : {rerank_ok}/{n}", flush=True)
    print("=" * 74, flush=True)

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(
        json.dumps({"resumen": resumen, "queries": filas}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Guardado: {OUT}", flush=True)


if __name__ == "__main__":
    main()
