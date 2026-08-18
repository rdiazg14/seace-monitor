#!/usr/bin/env python3
"""
G4 — Evaluación de retrieval v2 (Gemini).

Mide precisión/recall del vector search SIN llamar al LLM.
  v2 (default): Gemini 1536 + buscar_tdr_v2 + RRF
  v1: BGE 768 — deshabilitado (POST /embed apagado)

Uso:
  python eval_retrieval.py
  python eval_retrieval.py --backend v2 --k 10
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_EMBED_MODEL = "gemini-embedding-001"
RRF_K = 60
FTS_LIMITE = 20
OUT_DIR = Path(__file__).parent / "data"

# Réplica de extraerTermino() del Worker (v1).
_STOP = (
    r"que|qué|cuales|cuáles|hay|contratos?|contrataciones?|vigentes?|"
    r"culminad\w*|piden?|pide|compra|compran|requisitos?|para|"
    r"proveedores?|del|de|la|el|los|las|un|una|unos|unas|con|o|y|en|por"
)


def extraer_termino(query: str) -> str:
    t = query.lower()
    t = re.sub(r"[¿?¡!.,;:()]", " ", t)
    t = re.sub(rf"\b({_STOP})\b", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


def norm(s: str) -> str:
    t = unicodedata.normalize("NFKD", s or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.lower()


# must: al menos UN término. exclude: si aparece, el hit NO es relevante.
QUERIES: list[dict] = [
    {"id": "contador", "query": "trabajos de contador",
     "intent": "Servicios de contabilidad / contador",
     "must": ["contador", "contabilidad", "contable"], "exclude": []},
    {"id": "laptops", "query": "laptops",
     "intent": "Adquisición de laptops/notebooks",
     "must": ["laptop", "notebook", "computadora portatil"], "exclude": []},
    {"id": "equipos_computo", "query": "equipos de computo",
     "intent": "PCs, laptops u equipos de cómputo",
     "must": ["computo", "computadora", "laptop", "pc ", "equipo de computo"],
     "exclude": []},
    {"id": "ciberseguridad", "query": "ciberseguridad",
     "intent": "Ciberseguridad / seguridad de la información",
     "must": ["ciberseguridad", "ciber", "seguridad de la informacion",
              "firewall", "antivirus", "soc "],
     "exclude": ["extintor"]},
    {"id": "ciber_vigentes", "query": "contratos de ciberseguridad vigentes",
     "intent": "Ciberseguridad, preferible vigente",
     "must": ["ciberseguridad", "ciber", "seguridad de la informacion"],
     "exclude": ["extintor"]},
    {"id": "cloud", "query": "cloud computing",
     "intent": "Cloud / computación en la nube",
     "must": ["cloud", "nube", "aws", "azure", "hosting"], "exclude": []},
    {"id": "nube", "query": "servicio de cloud o nube",
     "intent": "Servicios en la nube",
     "must": ["cloud", "nube", "hosting", "aws", "azure"], "exclude": []},
    {"id": "camaras", "query": "camaras de vigilancia",
     "intent": "Cámaras / videovigilancia",
     "must": ["camara", "videovigilancia", "cctv", "vigilancia"], "exclude": []},
    {"id": "videovigilancia", "query": "videovigilancia",
     "intent": "Sistemas de videovigilancia",
     "must": ["videovigilancia", "camara", "cctv"], "exclude": []},
    {"id": "firewall", "query": "firewall",
     "intent": "Firewall / perímetro",
     "must": ["firewall", "cortafuego", "fortinet", "palo alto"], "exclude": []},
    {"id": "antivirus", "query": "antivirus",
     "intent": "Antivirus / EDR",
     "must": ["antivirus", "antimalware", "edr", "kaspersky", "eset"], "exclude": []},
    {"id": "microsoft365", "query": "microsoft 365",
     "intent": "Licencias o servicios Microsoft 365",
     "must": ["microsoft", "office 365", "m365", "365"], "exclude": []},
    {"id": "oracle", "query": "licencias oracle",
     "intent": "Oracle (BD o licencias)",
     "must": ["oracle"], "exclude": []},
    {"id": "desarrollo", "query": "desarrollo de software",
     "intent": "Desarrollo de software / sistemas",
     "must": ["desarrollo de software", "desarrollo de sistema", "aplicativo",
              "software a medida"],
     "exclude": []},
    {"id": "cableado", "query": "cableado estructurado",
     "intent": "Cableado / redes físicas",
     "must": ["cableado", "estructurado", "fibra optica"], "exclude": []},
    {"id": "impresoras", "query": "impresoras",
     "intent": "Impresoras / multifuncionales",
     "must": ["impresora", "multifuncional", "toner", "tinta"], "exclude": []},
    {"id": "firma_digital", "query": "firma digital",
     "intent": "Firma digital / certificados",
     "must": ["firma digital", "certificado digital", "reniec"], "exclude": []},
    {"id": "token_crypto", "query": "token criptografico",
     "intent": "Token criptográfico / firma",
     "must": ["token", "criptografico", "firma digital"], "exclude": []},
    {"id": "hosting", "query": "hosting",
     "intent": "Hosting / alojamiento web",
     "must": ["hosting", "alojamiento", "nube", "cloud"], "exclude": []},
    {"id": "backup", "query": "backup copias de respaldo",
     "intent": "Backup / copias de seguridad",
     "must": ["backup", "respaldo", "copia de seguridad"], "exclude": []},
    {"id": "switch", "query": "switch de red",
     "intent": "Switches de red",
     "must": ["switch", "conmutador", "red de datos"], "exclude": []},
    {"id": "core_i5", "query": "equipos de computo core i5",
     "intent": "PCs/laptops con Core i5",
     "must": ["core i5", "i5", "computo", "laptop", "computadora"], "exclude": []},
    {"id": "helpdesk", "query": "mesa de ayuda helpdesk",
     "intent": "Mesa de ayuda / soporte",
     "must": ["mesa de ayuda", "helpdesk", "help desk", "soporte tecnico"],
     "exclude": []},
    {"id": "ia", "query": "inteligencia artificial",
     "intent": "IA / analytics / LLM",
     "must": ["inteligencia artificial", "machine learning", "llm", "openai",
              "chatgpt", "ia generativa"],
     "exclude": []},
    {"id": "sap", "query": "sap erp",
     "intent": "SAP / ERP",
     "must": [" sap", "erp"], "exclude": []},
    {"id": "sqlserver", "query": "base de datos sql server",
     "intent": "SQL Server / bases de datos",
     "must": ["sql server", "base de datos", "postgresql", "mysql"], "exclude": []},
    {"id": "correo", "query": "correo electronico",
     "intent": "Correo electrónico institucional",
     "must": ["correo electronico", "email", "exchange", "outlook"], "exclude": []},
    {"id": "ups", "query": "ups energia equipos de computo",
     "intent": "UPS para equipos informáticos",
     "must": [" ups", "ups ", "energia ininterrumpida"], "exclude": []},
    {"id": "ssl", "query": "certificado ssl",
     "intent": "Certificados SSL/TLS",
     "must": ["ssl", "tls", "certificado digital"], "exclude": []},
    {"id": "adquisicion_laptops", "query": "adquisicion de laptops o equipos de computo",
     "intent": "Compra de laptops/cómputo (no cualquier 'adquisicion de')",
     "must": ["laptop", "computo", "computadora", "notebook"],
     "exclude": ["tinta", "camisa", "cronometro", "extintor"]},
]


def es_relevante(blob: str, q: dict) -> bool:
    n = " " + norm(blob) + " "
    if any(norm(x) and norm(x) in n for x in q.get("exclude") or []):
        return False
    return any(norm(x) and norm(x) in n for x in q["must"])


def embed_lote(client: httpx.Client, texts: list[str]) -> list[list[float]]:
    r = client.post(EMBED_URL, json={"texts": texts}, timeout=90.0)
    r.raise_for_status()
    embs = r.json()["embeddings"]
    if not isinstance(embs, list) or len(embs) != len(texts):
        raise RuntimeError("embed: respuesta inesperada")
    return embs


def l2_normalize(vec: list[float]) -> list[float]:
    s = sum(x * x for x in vec) ** 0.5
    if s <= 0:
        return vec
    return [x / s for x in vec]


def embed_query_gemini(client: httpx.Client, text: str) -> list[float]:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_EMBED_MODEL}:embedContent"
    )
    r = client.post(
        url,
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
        json={
            "model": f"models/{GEMINI_EMBED_MODEL}",
            "content": {"parts": [{"text": text}]},
            "taskType": "RETRIEVAL_QUERY",
            "outputDimensionality": 1536,
        },
        timeout=60.0,
    )
    r.raise_for_status()
    vals = (r.json().get("embedding") or {}).get("values") or []
    if len(vals) > 1536:
        vals = vals[:1536]
    if len(vals) != 1536:
        raise RuntimeError(f"gemini query dim {len(vals)} != 1536")
    return l2_normalize([float(x) for x in vals])


def buscar_v1(supa, vec: list[float], k: int, min_sim: float) -> list[dict]:
    res = supa.rpc("buscar_tdr", {
        "query_embedding": vec,
        "match_count": k,
        "filter_estado": None,
        "min_similarity": min_sim,
    }).execute()
    return res.data or []


def buscar_v2_vector(supa, vec: list[float], k: int, min_sim: float) -> list[dict]:
    res = supa.rpc("buscar_tdr_v2", {
        "query_embedding": vec,
        "match_count": k,
        "filter_estado": "Vigente",
        "min_similarity": min_sim,
    }).execute()
    return res.data or []


def rrf_fusion(vector_hits: list[dict], fts_ids: list[int], k: int) -> list[dict]:
    """RRF siempre: vector (chunks) + FTS (contrato_id). Devuelve hasta k chunks/filas."""
    scores: dict[int, float] = {}
    best_chunk: dict[int, dict] = {}

    seen_v: list[int] = []
    for h in vector_hits:
        cid = int(h["contrato_id"])
        if cid not in best_chunk:
            best_chunk[cid] = h
            seen_v.append(cid)
    for rank, cid in enumerate(seen_v, 1):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank)

    seen_f: list[int] = []
    for cid in fts_ids:
        if cid not in seen_f:
            seen_f.append(cid)
    for rank, cid in enumerate(seen_f, 1):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank)
        if cid not in best_chunk:
            best_chunk[cid] = {
                "contrato_id": cid,
                "chunk_index": -1,
                "tipo": "FTS",
                "texto": "",
                "similarity": 0.0,
                "fuente": "fts",
            }

    ordered = sorted(scores, key=lambda c: -scores[c])[:k]
    out = []
    for cid in ordered:
        row = dict(best_chunk[cid])
        row["rrf"] = round(scores[cid], 6)
        out.append(row)
    return out


def gold_fts(supa, termino: str, n: int = 20) -> list[int]:
    if not termino:
        return []
    res = supa.rpc("buscar_contratos", {
        "termino": termino,
        "filtro_objeto": None,
        "filtro_estado": "Vigente",
        "filtro_entidad": None,
        "limite": n,
        "offset_val": 0,
    }).execute()
    return [int(r["id"]) for r in (res.data or []) if r.get("id")]


def fetch_contratos(supa, ids: list[int]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    uniq = list(dict.fromkeys(ids))
    for i in range(0, len(uniq), 80):
        lote = uniq[i:i + 80]
        res = (
            supa.table("contratos")
            .select("id,estado,categoria_it,descripcion,descripcion_contrato")
            .in_("id", lote)
            .execute()
        )
        for row in res.data or []:
            out[int(row["id"])] = row
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["v1", "v2"], default="v2",
                    help="v2 = Gemini + buscar_tdr_v2 + RRF (default). v1 = BGE (deshabilitado).")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--min-similarity", type=float, default=None)
    args = ap.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SystemExit("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY no encontrados")
    if args.backend == "v1":
        raise SystemExit(
            "ERROR: --backend v1 (BGE /embed) está apagado. Usa el default --backend v2."
        )
    if not GEMINI_API_KEY:
        raise SystemExit("ERROR: GEMINI_API_KEY requerido para --backend v2")

    supa = create_client(SUPABASE_URL, SUPABASE_KEY)
    k = args.k
    v2 = args.backend == "v2"
    min_sim = args.min_similarity if args.min_similarity is not None else (0.20 if v2 else 0.70)

    print("=" * 72, flush=True)
    if v2:
        print("G4 eval retrieval  backend=v2 (Gemini 1536 + buscar_tdr_v2 + RRF)", flush=True)
        print("  reranker: no (solo Worker). Eval = híbrido pre-rerank.", flush=True)
    else:
        print("G4 eval retrieval  backend=v1 (BGE 768 + buscar_tdr)", flush=True)
    print(f"  k={k}  min_similarity={min_sim}  n_queries={len(QUERIES)}", flush=True)
    print("=" * 72, flush=True)

    terminos = [extraer_termino(q["query"]) or q["query"] for q in QUERIES]
    t_emb0 = time.time()
    vecs: list[list[float]] = []
    with httpx.Client() as http:
        if v2:
            for t in terminos:
                vecs.append(embed_query_gemini(http, t))
                time.sleep(0.15)
        else:
            for i in range(0, len(terminos), 15):
                vecs.extend(embed_lote(http, terminos[i:i + 15]))
                time.sleep(0.2)
    emb_s = time.time() - t_emb0
    print(f"  embeddings: {len(vecs)} en {emb_s:.1f}s", flush=True)

    filas = []
    t0 = time.time()
    for q, termino, vec in zip(QUERIES, terminos, vecs):
        if v2:
            vec_hits = buscar_v2_vector(supa, vec, 20, min_sim)
            fts_ids = gold_fts(supa, termino or q["must"][0], n=FTS_LIMITE)
            hits = rrf_fusion(vec_hits, fts_ids, k)
        else:
            hits = buscar_v1(supa, vec, k, min_sim)
        gold = gold_fts(supa, q["must"][0], n=20)
        ids = [int(h["contrato_id"]) for h in hits]
        meta = fetch_contratos(supa, ids + gold)
        judged = []
        for rank, h in enumerate(hits, 1):
            cid = int(h["contrato_id"])
            c = meta.get(cid) or {}
            blob = " ".join([
                h.get("texto") or "",
                c.get("descripcion") or "",
                c.get("descripcion_contrato") or "",
                c.get("categoria_it") or "",
            ])
            rel = es_relevante(blob, q)
            judged.append({
                "rank": rank,
                "contrato_id": cid,
                "similarity": round(float(h.get("similarity") or 0), 4),
                "rrf": h.get("rrf"),
                "tipo": h.get("tipo"),
                "estado": c.get("estado"),
                "categoria_it": c.get("categoria_it"),
                "relevante": rel,
                "texto": (h.get("texto") or c.get("descripcion") or "")[:160],
            })
        rel_n = sum(1 for j in judged if j["relevante"])
        p_at = rel_n / max(len(judged), 1) if judged else 0.0
        p5 = (sum(1 for j in judged[:5] if j["relevante"]) / min(5, max(len(judged), 1))
              if judged else 0.0)
        inter = set(ids) & set(gold)
        recall = (len(inter) / len(gold)) if gold else None
        success = rel_n >= 1
        filas.append({
            "id": q["id"],
            "query": q["query"],
            "termino": termino,
            "intent": q["intent"],
            "n_hits": len(hits),
            "n_relevantes": rel_n,
            "p@5": round(p5, 3),
            "p@k": round(p_at, 3),
            "success@k": success,
            "gold_fts": len(gold),
            "recall_vs_fts": None if recall is None else round(recall, 3),
            "hits": judged,
        })
        flag = "OK" if success else "MISS"
        print(
            f"  [{flag}] {q['id']:<22} hits={len(hits):2} rel={rel_n}  "
            f"P@5={p5:.2f} P@{k}={p_at:.2f}  gold={len(gold)}  "
            f"R_fts={'-' if recall is None else f'{recall:.2f}'}",
            flush=True,
        )

    n = len(filas)
    n_ok = sum(1 for f in filas if f["success@k"])
    macro_p5 = sum(f["p@5"] for f in filas) / n
    macro_pk = sum(f["p@k"] for f in filas) / n
    recs = [f["recall_vs_fts"] for f in filas if f["recall_vs_fts"] is not None]
    macro_r = sum(recs) / len(recs) if recs else None
    vacios = sum(1 for f in filas if f["n_hits"] == 0)

    resumen = {
        "backend": "v2" if v2 else "v1",
        "modelo": "gemini-embedding-001@1536" if v2 else "bge-base-en-v1.5",
        "rpc": "buscar_tdr_v2+RRF" if v2 else "buscar_tdr",
        "k": k,
        "min_similarity": min_sim,
        "n_queries": n,
        "success@k": n_ok / n,
        "macro_P@5": round(macro_p5, 3),
        "macro_P@k": round(macro_pk, 3),
        "macro_R_vs_fts": None if macro_r is None else round(macro_r, 3),
        "queries_sin_hits": vacios,
        "elapsed_s": round(time.time() - t0 + emb_s, 1),
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    label = "EVAL v2" if v2 else "BASELINE v1"
    print("\n" + "=" * 72, flush=True)
    print(label, flush=True)
    print(f"  success@{k}     : {n_ok}/{n} ({100 * n_ok / n:.0f}%)", flush=True)
    print(f"  macro P@5      : {macro_p5:.3f}", flush=True)
    print(f"  macro P@{k:<2}     : {macro_pk:.3f}", flush=True)
    print(f"  macro R vs FTS : {macro_r if macro_r is not None else '-'}", flush=True)
    print(f"  queries 0 hits : {vacios}", flush=True)
    print("=" * 72, flush=True)

    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / ("eval_v2.json" if v2 else "eval_baseline_v1.json")
    out.write_text(
        json.dumps({"resumen": resumen, "queries": filas}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Guardado: {out}", flush=True)


if __name__ == "__main__":
    main()
