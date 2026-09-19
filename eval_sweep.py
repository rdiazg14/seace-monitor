#!/usr/bin/env python3
"""
G4c — sweep de hiperparámetros del retrieval (threshold / RRF_K) + top_k del reranker.

Mide, SIN re-embebar, cuál combinación maximiza success@10 / P@5 / P@10.
Cachea embeddings + vector hits (min_sim=0.0) + FTS para que el barrido sea barato.
El reranker se mide solo para las mejores combinaciones (top_k = 5 y 10).

Uso: python eval_sweep.py
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

import eval_retrieval as ev
import eval_rerank as er

supa = er.supa
CACHE = Path(__file__).parent / "data" / "sweep_cache.json"
OUT = Path(__file__).parent / "data" / "eval_sweep.json"

THRESHOLDS = [0.10, 0.15, 0.20, 0.25, 0.30]
RRF_KS = [20, 40, 60, 100]
TOP_KS = [5, 10]
# Combos a medir con reranker (post-rerank) además del barrido pre-rerank.
RERANK_COMBOS = [(0.20, 60), (0.15, 60), (0.20, 40), (0.25, 60)]


def build_cache() -> dict:
    terminos = [ev.extraer_termino(q["query"]) or q["query"] for q in ev.QUERIES]
    vecs: list[list[float]] = []
    with httpx.Client() as http:
        for t in terminos:
            vecs.append(ev.embed_query_gemini(http, t))
            time.sleep(0.15)
    cache: dict = {"embeddings": vecs, "queries": []}
    for q, termino, vec in zip(ev.QUERIES, terminos, vecs):
        vec_hits = ev.buscar_v2_vector(supa, vec, 20, 0.0)
        fts_ids = ev.gold_fts(supa, termino or q["must"][0], n=20)
        cache["queries"].append({
            "id": q["id"],
            "query": q["query"],
            "termino": termino,
            "vec_hits": vec_hits,
            "fts_ids": fts_ids,
        })
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return cache


def build_top20(vec_hits: list[dict], fts_ids: list[int], min_sim: float, rrf_k: int) -> list[dict]:
    vec_hits_f = [h for h in vec_hits if (h.get("similarity") or 0) >= min_sim]
    vec_by_cid: dict[int, dict] = {}
    vec_order: list[int] = []
    for h in vec_hits_f:
        cid = int(h["contrato_id"])
        if cid not in vec_by_cid:
            vec_by_cid[cid] = h
            vec_order.append(cid)
    scores: dict[int, float] = {}
    best: dict[int, dict] = {}
    for rank, cid in enumerate(vec_order, 1):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (rrf_k + rank)
        best[cid] = vec_by_cid[cid]
    for rank, cid in enumerate(fts_ids, 1):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (rrf_k + rank)
        if cid not in best:
            best[cid] = {
                "contrato_id": cid, "chunk_index": -1, "tipo": "FTS",
                "texto": "", "similarity": 0.0, "fuente": "fts",
            }
    fused = sorted(scores, key=lambda c: -scores[c])[:20]
    candidates: list[dict] = []
    for cid in fused:
        chunks_cid = [h for h in vec_hits_f if int(h["contrato_id"]) == cid]
        if chunks_cid:
            candidates.extend(chunks_cid[:2])
        else:
            candidates.append(best[cid])
    return candidates[:20]


def metrics(top20: list[dict], q: dict, meta: dict[int, dict]) -> dict:
    rel = er.judge(top20, q, meta)
    n = len(rel)
    p5 = sum(1 for x in rel[:5] if x) / min(5, n) if n else 0.0
    p10 = sum(1 for x in rel[:10] if x) / min(10, n) if n else 0.0
    success10 = 1 if any(rel[:10]) else 0
    return {"p5": p5, "p10": p10, "success10": success10, "rel_n": sum(rel)}


def main():
    if CACHE.exists():
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
        print(f"[cache] reutilizando {CACHE.name} ({len(cache['queries'])} queries)", flush=True)
    else:
        print("Construyendo caché (embeddings + vector + fts)...", flush=True)
        cache = build_cache()

    qs = cache["queries"]
    orig = {q["id"]: q for q in ev.QUERIES}
    print(f"Barriendo {len(THRESHOLDS)} thresholds x {len(RRF_KS)} RRF_K = "
          f"{len(THRESHOLDS) * len(RRF_KS)} combos x {len(qs)} queries", flush=True)

    # Pre-cargar metadata de todos los ids candidatos (una sola vez).
    all_ids: set[int] = set()
    for q in qs:
        for h in q["vec_hits"]:
            all_ids.add(int(h["contrato_id"]))
        all_ids.update(q["fts_ids"])
    meta = er.fetch_meta(sorted(all_ids))
    print(f"  metadata contratos: {len(meta)}", flush=True)

    filas: list[dict] = []
    for min_sim in THRESHOLDS:
        for rrf_k in RRF_KS:
            p5s, p10s, s10s, rel_tot = [], [], [], 0
            for q in qs:
                full = orig[q["id"]]
                top20 = build_top20(q["vec_hits"], q["fts_ids"], min_sim, rrf_k)
                m = metrics(top20, full, meta)
                p5s.append(m["p5"])
                p10s.append(m["p10"])
                s10s.append(m["success10"])
                rel_tot += m["rel_n"]
            n = len(qs)
            filas.append({
                "min_sim": min_sim,
                "rrf_k": rrf_k,
                "success10": sum(s10s) / n,
                "macro_P@5": sum(p5s) / n,
                "macro_P@10": sum(p10s) / n,
                "total_relevantes": rel_tot,
            })

    # Ordenar por success@10 (comparable al 63 %) y P@5.
    filas_sorted = sorted(filas, key=lambda f: (-f["success10"], -f["macro_P@5"]))
    print("\n=== SWEEP pre-rerank (threshold x RRF_K) ===", flush=True)
    print(f"{'min_sim':>8} {'rrf_k':>6} {'success@10':>10} {'P@5':>6} {'P@10':>6} {'rel':>5}", flush=True)
    for f in filas_sorted:
        mark = " <-- prod" if (f["min_sim"] == 0.20 and f["rrf_k"] == 60) else ""
        print(f"{f['min_sim']:>8.2f} {f['rrf_k']:>6} {f['success10']:>10.3f} "
              f"{f['macro_P@5']:>6.3f} {f['macro_P@10']:>6.3f} {f['total_relevantes']:>5}{mark}",
              flush=True)

    # Reranker post-rerank para combos elegidos.
    print("\n=== POST-rerank (bge-reranker-base) para combos elegidos ===", flush=True)
    post_rows: list[dict] = []
    with httpx.Client() as http:
        for min_sim, rrf_k in RERANK_COMBOS:
            for topk in TOP_KS:
                p5s, p10s, s10s, ok = [], [], [], 0
                for q in qs:
                    full = orig[q["id"]]
                    top20 = build_top20(q["vec_hits"], q["fts_ids"], min_sim, rrf_k)
                    contexts = [(h.get("texto") or "") for h in top20]
                    rr_idx = er.rerank(http, q["termino"] or q["query"], contexts, topk)
                    if rr_idx:
                        ok += 1
                        post_items = [top20[i] for i in rr_idx if i < len(top20)]
                        seen = {i for i in rr_idx if i < len(top20)}
                        post_items += [top20[i] for i in range(len(top20)) if i not in seen]
                    else:
                        post_items = top20
                    m = metrics(post_items, full, meta)
                    p5s.append(m["p5"])
                    p10s.append(m["p10"])
                    s10s.append(m["success10"])
                n = len(qs)
                row = {
                    "min_sim": min_sim, "rrf_k": rrf_k, "top_k": topk,
                    "success10": sum(s10s) / n,
                    "macro_P@5": sum(p5s) / n,
                    "macro_P@10": sum(p10s) / n,
                    "rerank_ok": ok,
                }
                post_rows.append(row)
                print(f"  min_sim={min_sim:.2f} rrf_k={rrf_k:>3} top_k={topk:>2}  "
                      f"success@10={row['success10']:.3f} P@5={row['macro_P@5']:.3f} "
                      f"P@10={row['macro_P@10']:.3f}  rerank_ok={ok}/{n}", flush=True)

    best_pre = filas_sorted[0]
    best_post = max(post_rows, key=lambda r: (-r["success10"], -r["macro_P@5"]))
    resumen = {
        "baseline_referencia": "success@10 pre-rerank prod (0.20/60)",
        "prod_success10": next(f["success10"] for f in filas if f["min_sim"] == 0.20 and f["rrf_k"] == 60),
        "best_pre": best_pre,
        "best_post": best_post,
        "sweep": filas_sorted,
        "post_rerank": post_rows,
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nMejor pre-rerank : min_sim={best_pre['min_sim']:.2f} rrf_k={best_pre['rrf_k']} "
          f"success@10={best_pre['success10']:.3f}", flush=True)
    print(f"Mejor post-rerank: min_sim={best_post['min_sim']:.2f} rrf_k={best_post['rrf_k']} "
          f"top_k={best_post['top_k']} success@10={best_post['success10']:.3f}", flush=True)
    print(f"Guardado: {OUT}", flush=True)


if __name__ == "__main__":
    main()
