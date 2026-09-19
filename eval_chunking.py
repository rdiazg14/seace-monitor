#!/usr/bin/env python3
"""
eval_chunking.py — A/B de estrategias de chunking del TDR (fuente=pdf).

Mide, SIN tocar producción, si re-chunkear el TDR (tamaño + overlap) mejora el
retrieval. Solo varía la fuente `pdf` (descripción/ítems/metadata = fuente `api`
no cambian). Replica fielmente el pipeline de producción:

  embed query (gemini-embedding-001, RETRIEVAL_QUERY, 1536, L2)
  vector search (coseno = dot, top-20, sim > 0.20) sobre api + pdf
  + FTS (buscar_contratos, top-20) -> RRF k=60 a nivel contrato
  -> candidates (<=2 chunks/contrato, top-20) -> judge (es_relevante)

Regla de decisión: solo aplicar la variante ganadora si NO empeora el baseline.

Uso:
  python eval_chunking.py --baseline
  python eval_chunking.py --variant 300_0
  python eval_chunking.py --variant 300_60
  python eval_chunking.py --variant 250_50
  python eval_chunking.py --report
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import httpx
import numpy as np
import psycopg
from supabase import create_client

import eval_retrieval as ev
import generar_embeddings as ge
from chunker_contratos import (
    approx_tokens,
    con_contexto_pdf,
    embed_text_pdf,
    meta_de_contrato,
    split_por_parrafos,
    MAX_TOKENS_ANTES_SPLIT,
)

DATA = Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)

DSN = os.environ.get("DATABASE_URL", "").strip()
MIN_SIM = 0.20
VEC_K = 20
FTS_K = 20
RRF_K = ev.RRF_K
TOP_CANDIDATES = 20
MAX_CHARS_GEMINI = ge.MAX_CHARS_GEMINI  # 8000

VARIANTES: dict[str, tuple[int, int]] = {
    "300_0": (300, 0),
    "300_60": (300, 60),
    "250_50": (250, 50),
}

# ---------- cache de corpus base ----------
C_CORPUS = {
    "contratos": DATA / "corpus_contratos.json",
    "clasif": DATA / "corpus_clasif.json",
    "api_emb": DATA / "corpus_api_emb.npy",
    "api_meta": DATA / "corpus_api_meta.json",
    "pdf_emb": DATA / "corpus_pdf_emb.npy",
    "pdf_meta": DATA / "corpus_pdf_meta.json",
}
C_QUERY_EMB = DATA / "query_emb.json"
C_QUERY_FTS = DATA / "query_fts.json"


def parse_vec(s: str) -> np.ndarray:
    return np.array(json.loads(s), dtype=np.float32)


def cargar_corpus() -> dict:
    if all(p.exists() for p in C_CORPUS.values()):
        return {
            "contratos": json.loads(C_CORPUS["contratos"].read_text(encoding="utf-8")),
            "clasif": json.loads(C_CORPUS["clasif"].read_text(encoding="utf-8")),
            "api_emb": np.load(C_CORPUS["api_emb"]),
            "api_meta": json.loads(C_CORPUS["api_meta"].read_text(encoding="utf-8")),
            "pdf_emb": np.load(C_CORPUS["pdf_emb"]),
            "pdf_meta": json.loads(C_CORPUS["pdf_meta"].read_text(encoding="utf-8")),
        }

    print("Cargando corpus desde BD (contratos + chunks api/pdf + clasificación)...", flush=True)
    if not DSN:
        raise SystemExit("ERROR: DATABASE_URL no encontrado")
    t0 = time.time()
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("SET statement_timeout = '300s'")

        rows = conn.execute(
            "SELECT id, entidad, descripcion, objeto, descripcion_contrato, tdr_texto, estado "
            "FROM contratos WHERE estado = 'Vigente'"
        ).fetchall()
        contratos = {}
        for r in rows:
            contratos[str(r[0])] = {
                "id": r[0], "entidad": r[1], "descripcion": r[2], "objeto": r[3],
                "descripcion_contrato": r[4], "tdr_texto": r[5], "estado": r[6],
            }
        print(f"  contratos vigentes: {len(contratos):,}  ({time.time()-t0:.1f}s)", flush=True)

        rows = conn.execute(
            "SELECT contrato_id, categoria_it FROM clasificacion_contrato "
            "WHERE contrato_id IN (SELECT id FROM contratos WHERE estado='Vigente')"
        ).fetchall()
        clasif = {str(r[0]): r[1] for r in rows}
        print(f"  clasificacion: {len(clasif):,}", flush=True)

        rows = conn.execute(
            "SELECT ch.contrato_id, ch.texto, ch.embedding_v2::text "
            "FROM chunks_tdr ch JOIN contratos c ON c.id = ch.contrato_id "
            "WHERE c.estado='Vigente' AND ch.fuente='api' AND ch.embedding_v2 IS NOT NULL"
        ).fetchall()
        api_emb = np.vstack([parse_vec(r[2]) for r in rows]) if rows else np.zeros((0, 1536), np.float32)
        api_meta = [{"contrato_id": int(r[0]), "texto": r[1], "fuente": "api"} for r in rows]
        print(f"  chunks api: {len(rows):,}  ({time.time()-t0:.1f}s)", flush=True)

        rows = conn.execute(
            "SELECT ch.contrato_id, ch.texto, ch.chunk_embed_text, ch.embedding_v2::text "
            "FROM chunks_tdr ch JOIN contratos c ON c.id = ch.contrato_id "
            "WHERE c.estado='Vigente' AND ch.fuente='pdf' AND ch.embedding_v2 IS NOT NULL"
        ).fetchall()
        pdf_emb = np.vstack([parse_vec(r[3]) for r in rows]) if rows else np.zeros((0, 1536), np.float32)
        pdf_meta = [{"contrato_id": int(r[0]), "texto": r[1], "fuente": "pdf"} for r in rows]
        print(f"  chunks pdf: {len(rows):,}  ({time.time()-t0:.1f}s)", flush=True)

    C_CORPUS["contratos"].write_text(json.dumps(contratos, ensure_ascii=False), encoding="utf-8")
    C_CORPUS["clasif"].write_text(json.dumps(clasif, ensure_ascii=False), encoding="utf-8")
    np.save(C_CORPUS["api_emb"], api_emb)
    C_CORPUS["api_meta"].write_text(json.dumps(api_meta, ensure_ascii=False), encoding="utf-8")
    np.save(C_CORPUS["pdf_emb"], pdf_emb)
    C_CORPUS["pdf_meta"].write_text(json.dumps(pdf_meta, ensure_ascii=False), encoding="utf-8")
    print(f"Corpus cacheado en {time.time()-t0:.1f}s", flush=True)

    return {
        "contratos": contratos, "clasif": clasif,
        "api_emb": api_emb, "api_meta": api_meta,
        "pdf_emb": pdf_emb, "pdf_meta": pdf_meta,
    }


def meta_contrato(corpus: dict) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for sid, c in corpus["contratos"].items():
        cid = int(sid)
        out[cid] = {
            "descripcion": c.get("descripcion") or "",
            "descripcion_contrato": c.get("descripcion_contrato") or "",
            "categoria_it": corpus["clasif"].get(sid) or "",
        }
    return out


def cargar_queries() -> tuple[list[list[float]], list[str], list[list[int]]]:
    """Cachea embeddings de query + FTS para consistencia entre corridas."""
    terminos = [ev.extraer_termino(q["query"]) or q["query"] for q in ev.QUERIES]

    if C_QUERY_EMB.exists() and C_QUERY_FTS.exists():
        vecs = json.loads(C_QUERY_EMB.read_text(encoding="utf-8"))
        fts = json.loads(C_QUERY_FTS.read_text(encoding="utf-8"))
        return vecs, terminos, fts

    print("Embeber queries (30) + FTS...", flush=True)
    supa = create_client(ev.SUPABASE_URL, ev.SUPABASE_KEY)
    vecs: list[list[float]] = []
    with httpx.Client() as http:
        for t in terminos:
            vecs.append(ev.embed_query_gemini(http, t))
            time.sleep(0.15)
    fts: list[list[int]] = []
    for q, termino in zip(ev.QUERIES, terminos):
        fts.append(ev.gold_fts(supa, termino or q["must"][0], n=FTS_K))

    C_QUERY_EMB.write_text(json.dumps(vecs), encoding="utf-8")
    C_QUERY_FTS.write_text(json.dumps(fts), encoding="utf-8")
    return vecs, terminos, fts


def split_parrafos_overlap(texto: str, target_tokens: int, overlap_tokens: int) -> list[str]:
    if overlap_tokens <= 0:
        return split_por_parrafos(texto, target_tokens)
    partes = [p.strip() for p in texto.replace("\r\n", "\n").split("\n") if p.strip()]
    if not partes:
        return [texto.strip()] if texto.strip() else []
    chunks: list[str] = []
    buf: list[str] = []
    buf_tok = 0
    for p in partes:
        pt = approx_tokens(p)
        buf.append(p)
        buf_tok += pt
        if buf_tok >= target_tokens:
            chunks.append("\n".join(buf))
            ov: list[str] = []
            ov_tok = 0
            for pp in reversed(buf):
                ov_tok += approx_tokens(pp)
                ov.append(pp)
                if ov_tok >= overlap_tokens:
                    break
            ov.reverse()
            buf = ov
            buf_tok = ov_tok
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def chunks_pdf_variant(c: dict, target_tokens: int, overlap_tokens: int) -> list[dict]:
    tdr = (c.get("tdr_texto") or "").strip()
    if not tdr:
        return []
    cid = int(c["id"])
    if approx_tokens(tdr) > MAX_TOKENS_ANTES_SPLIT:
        partes = split_parrafos_overlap(tdr, target_tokens, overlap_tokens)
    else:
        partes = [tdr]
    meta = meta_de_contrato(c)
    out: list[dict] = []
    for i, parte in enumerate(partes):
        texto = con_contexto_pdf(c, parte)
        row = {
            "contrato_id": cid,
            "chunk_index": i,
            "tipo": "TDR PDF" if len(partes) == 1 else f"TDR PDF ({i+1}/{len(partes)})",
            "texto": texto,
            "fuente": "pdf",
            "chunk_embed_text": embed_text_pdf(c, texto),
        }
        row.update(meta)
        out.append(row)
    return out


def top_k_chunks(emb: np.ndarray, meta: list[dict], qv: np.ndarray, k: int, min_sim: float) -> list[dict]:
    if emb.shape[0] == 0:
        return []
    sims = emb @ qv
    n = emb.shape[0]
    if n <= k:
        idx = np.argsort(-sims)
    else:
        idx = np.argpartition(-sims, k - 1)[:k]
        idx = idx[np.argsort(-sims[idx])]
    out: list[dict] = []
    for i in idx:
        s = float(sims[i])
        if s <= min_sim:
            break
        m = meta[int(i)]
        out.append({
            "contrato_id": m["contrato_id"],
            "texto": m["texto"],
            "similarity": s,
            "fuente": m["fuente"],
        })
    return out


def build_candidates(vec_hits: list[dict], fts_ids: list[int]) -> list[dict]:
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
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank)
        best[cid] = vec_by_cid[cid]
    for rank, cid in enumerate(fts_ids, 1):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank)
        if cid not in best:
            best[cid] = {
                "contrato_id": cid, "chunk_index": -1, "tipo": "FTS",
                "texto": "", "similarity": 0.0, "fuente": "fts",
            }

    fused = sorted(scores, key=lambda c: -scores[c])[:TOP_CANDIDATES]
    candidates: list[dict] = []
    for cid in fused:
        chunks_cid = [h for h in vec_hits if int(h["contrato_id"]) == cid]
        if chunks_cid:
            candidates.extend(chunks_cid[:2])
        else:
            candidates.append(best[cid])
    return candidates[:TOP_CANDIDATES]


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


def medir(emb: np.ndarray, meta: list[dict], mcont: dict[int, dict],
          vecs: list[list[float]], fts: list[list[int]]) -> dict:
    filas = []
    for q, vec, fts_ids in zip(ev.QUERIES, vecs, fts):
        qv = np.array(vec, dtype=np.float32)
        vec_hits = top_k_chunks(emb, meta, qv, VEC_K, MIN_SIM)
        candidates = build_candidates(vec_hits, fts_ids)
        rel = judge(candidates, q, mcont)
        filas.append({
            "id": q["id"],
            "n_vec": len(vec_hits),
            "n_cand": len(candidates),
            "P@5": round(p_at(rel, 5), 3),
            "P@10": round(p_at(rel, 10), 3),
            "rel_n": sum(rel),
            "success@10": 1 if any(rel[:10]) else 0,
        })
    n = len(filas)
    res = {
        "n_queries": n,
        "success@10": sum(f["success@10"] for f in filas) / n,
        "macro_P@5": sum(f["P@5"] for f in filas) / n,
        "macro_P@10": sum(f["P@10"] for f in filas) / n,
        "total_relevantes": sum(f["rel_n"] for f in filas),
    }
    return {"resumen": res, "queries": filas}


def print_resumen(tag: str, res: dict) -> None:
    r = res["resumen"]
    print("=" * 66, flush=True)
    print(f"{tag}", flush=True)
    print(f"  success@10 : {r['success@10']:.3f}  ({r['success@10']*100:.1f}%)", flush=True)
    print(f"  macro P@5  : {r['macro_P@5']:.3f}", flush=True)
    print(f"  macro P@10 : {r['macro_P@10']:.3f}", flush=True)
    print(f"  relevantes : {r['total_relevantes']}", flush=True)
    print("=" * 66, flush=True)


def run_baseline(corpus: dict) -> None:
    print("== BASELINE (chunks actuales 500/sin-overlap en BD) ==", flush=True)
    vecs, terminos, fts = cargar_queries()
    mcont = meta_contrato(corpus)
    emb = np.vstack([corpus["api_emb"], corpus["pdf_emb"]])
    meta = corpus["api_meta"] + corpus["pdf_meta"]
    res = medir(emb, meta, mcont, vecs, fts)
    print_resumen("BASELINE", res)
    out = DATA / "eval_chunking_base.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Guardado: {out}", flush=True)


def embed_pdf_resumable(texts: list[str], tag: str, delay: float) -> tuple[np.ndarray, bool]:
    """Embebe con resume en disco (memmap + progress). Devuelve (matriz, completo).

    - fail_fast=True: ante 429/errores PARA de inmediato y guarda progreso
      (no pierde chunks embebidos; se reanuda re-ejecutando el mismo comando).
    - Completo = True solo si se embebieron TODOS los chunks.
    """
    total = len(texts)
    dim = ge.GEMINI_DIM
    emb_path = DATA / f"chunk_emb_{tag}.npy"
    prog_path = DATA / f"chunk_emb_{tag}.progress.json"

    if emb_path.exists():
        mm = np.load(emb_path, mmap_mode="r+")
        if mm.shape != (total, dim):
            print(f"  [reset] shape {mm.shape} != ({total},{dim}); recreando", flush=True)
            del mm
            emb_path.unlink()
            mm = np.lib.format.open_memmap(emb_path, mode="w+", dtype=np.float32, shape=(total, dim))
    else:
        mm = np.lib.format.open_memmap(emb_path, mode="w+", dtype=np.float32, shape=(total, dim))

    done = 0
    if prog_path.exists():
        done = int(json.loads(prog_path.read_text(encoding="utf-8")).get("done", 0))
    done = max(0, min(done, total))
    print(f"  [resume] {tag}: {done}/{total} ya embebidos", flush=True)

    t0 = time.time()
    with httpx.Client() as http:
        i = done
        while i < total:
            lote = texts[i:i + ge.BATCH_GEMINI]
            try:
                vecs_lote = ge.embed_lote_gemini(http, lote, fail_fast=True)
            except ge.QuotaExceeded as e:
                print(f"  [429] lote {i}: {e}", flush=True)
                print(f"  progreso guardado: {i}/{total}. Reanuda re-ejecutando el mismo comando.", flush=True)
                mm.flush()
                return mm, False
            except Exception as e:
                print(f"  [error] lote {i}: {e}", flush=True)
                mm.flush()
                return mm, False
            n = len(vecs_lote)
            for j, v in enumerate(vecs_lote):
                mm[i + j] = np.array(v, dtype=np.float32)
            mm.flush()
            i += n
            prog_path.write_text(json.dumps({"done": i}), encoding="utf-8")
            if i % (ge.BATCH_GEMINI * 25) == 0 or i >= total:
                el = time.time() - t0
                print(f"    [{i}/{total}] {el:.0f}s  {i/max(el,1):.1f}/s", flush=True)
            time.sleep(delay)
    mm.flush()
    return mm, True


def run_variant(corpus: dict, tag: str, delay: float) -> None:
    target, overlap = VARIANTES[tag]
    print(f"== VARIANTE {tag}  (target={target} overlap={overlap}) ==", flush=True)
    vecs, terminos, fts = cargar_queries()
    mcont = meta_contrato(corpus)

    meta_path = DATA / f"chunk_emb_{tag}_meta.json"
    # Chunking determinístico (rápido): se regenera igual en cada resume.
    pdf_rows: list[dict] = []
    for sid, c in corpus["contratos"].items():
        pdf_rows.extend(chunks_pdf_variant(c, target, overlap))
    pdf_meta = [{"contrato_id": r["contrato_id"], "texto": r["texto"], "fuente": "pdf"} for r in pdf_rows]
    meta_path.write_text(json.dumps(pdf_meta, ensure_ascii=False), encoding="utf-8")
    print(f"  chunks pdf generados: {len(pdf_rows):,}", flush=True)

    texts = [(r["chunk_embed_text"] or "")[:MAX_CHARS_GEMINI] for r in pdf_rows]
    pdf_emb, completo = embed_pdf_resumable(texts, tag, delay)
    if not completo:
        print(f"  [incompleto] {tag}: eval no ejecutado. Progreso en disco; reanuda con el mismo comando.", flush=True)
        return

    emb = np.vstack([corpus["api_emb"], pdf_emb])
    meta = corpus["api_meta"] + pdf_meta
    res = medir(emb, meta, mcont, vecs, fts)
    print_resumen(f"VARIANTE {tag}", res)
    out = DATA / f"eval_chunking_{tag}.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Guardado: {out}", flush=True)


def run_report() -> None:
    base_path = DATA / "eval_chunking_base.json"
    if not base_path.exists():
        raise SystemExit("Sin baseline. Corre --baseline primero.")
    base = json.loads(base_path.read_text(encoding="utf-8"))["resumen"]
    print("=" * 74, flush=True)
    print(f"{'estrategia':<22} {'success@10':>10} {'P@5':>6} {'P@10':>6} {'rel':>5}", flush=True)
    print("-" * 74, flush=True)
    print(f"{'BASELINE 500/0':<22} {base['success@10']:>10.3f} {base['macro_P@5']:>6.3f} "
          f"{base['macro_P@10']:>6.3f} {base['total_relevantes']:>5}", flush=True)
    for tag in VARIANTES:
        p = DATA / f"eval_chunking_{tag}.json"
        if not p.exists():
            print(f"{tag:<22} (sin medir)", flush=True)
            continue
        r = json.loads(p.read_text(encoding="utf-8"))["resumen"]
        d10 = r["success@10"] - base["success@10"]
        d5 = r["macro_P@5"] - base["macro_P@5"]
        print(f"{tag:<22} {r['success@10']:>10.3f} {r['macro_P@5']:>6.3f} "
              f"{r['macro_P@10']:>6.3f} {r['total_relevantes']:>5}   "
              f"(d success@10 {d10:+.3f}, d P@5 {d5:+.3f})", flush=True)
    print("=" * 74, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--variant", default="", help="tag de variante (300_0|300_60|250_50)")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="Pausa entre lotes de embedding en segundos (default 0.5)")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if args.report:
        run_report()
        return

    corpus = cargar_corpus()
    if args.baseline:
        run_baseline(corpus)
    elif args.variant:
        if args.variant not in VARIANTES:
            raise SystemExit(f"variant desconocida: {args.variant} (usar {list(VARIANTES)})")
        run_variant(corpus, args.variant, args.delay)
    else:
        raise SystemExit("Elige --baseline, --variant TAG o --report")


if __name__ == "__main__":
    main()
