#!/usr/bin/env python3
"""
Auditoría RAG v2 — queries semánticas Gemini + buscar_tdr_v2 vs FTS.
Ejecutar: uv run python auditoria_rag.py
Guarda resultados en data/auditoria_rag.json
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
from supabase import create_client

from eval_retrieval import GEMINI_API_KEY, embed_query_gemini

_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise SystemExit("ERROR: .env sin credenciales")
if not GEMINI_API_KEY:
    raise SystemExit("ERROR: GEMINI_API_KEY requerido")

supa = create_client(SUPABASE_URL, SUPABASE_KEY)

QUERIES = [
    "equipo de computo laptop",
    "servicio de ciberseguridad firewall",
    "licencia de software microsoft",
    "servicio de cloud computing nube",
    "contratacion de tokens inteligencia artificial",
]


def buscar_vector(vec, n=3) -> list[dict]:
    res = supa.rpc("buscar_tdr_v2", {
        "query_embedding": vec,
        "match_count": n,
        "filter_estado": "Vigente",
        "min_similarity": 0.20,
    }).execute()
    return res.data or []


def buscar_fts(termino, n=3) -> list[dict]:
    res = supa.rpc("buscar_contratos", {
        "termino": termino,
        "filtro_objeto": None,
        "filtro_estado": "Vigente",
        "filtro_entidad": None,
        "limite": n,
        "offset_val": 0,
    }).execute()
    return res.data or []


print("=" * 65)
print("AUDITORÍA RAG v2 — Gemini + buscar_tdr_v2 vs FTS")
print("=" * 65)

resultados = {}

with httpx.Client() as http:
    for query in QUERIES:
        print(f"\n{'─'*65}")
        print(f"Query: \"{query}\"")
        t0 = time.time()
        vec = embed_query_gemini(http, query)
        embed_ms = (time.time() - t0) * 1000

        t1 = time.time()
        hits = buscar_vector(vec, n=3)
        search_ms = (time.time() - t1) * 1000

        print(f"  Embed: {embed_ms:.0f}ms  |  Search: {search_ms:.0f}ms")
        print(f"  Hits: {len(hits)}")

        hits_clean = []
        for i, h in enumerate(hits, 1):
            texto = (h.get("texto") or "")[:100]
            sim = h.get("similarity") or 0
            flag = "✓" if sim >= 0.20 else "✗"
            print(f"  [{i}] {flag} sim={sim:.3f} tipo={h.get('tipo')} fuente={h.get('fuente')} id={h.get('contrato_id')}")
            print(f"       {texto}")
            hits_clean.append({
                "rank": i,
                "contrato_id": h.get("contrato_id"),
                "chunk_index": h.get("chunk_index"),
                "tipo": h.get("tipo"),
                "fuente": h.get("fuente"),
                "similarity": round(sim, 4),
                "texto_100": texto,
            })

        resultados[query] = {
            "embed_ms": round(embed_ms),
            "search_ms": round(search_ms),
            "hits": hits_clean,
            "avg_similarity": round(
                sum(h["similarity"] for h in hits_clean) / max(len(hits_clean), 1), 4
            ),
        }

    print(f"\n{'─'*65}")
    print("COMPARACIÓN FTS vs Vector Search — 'ciberseguridad'")
    print("─" * 65)

    vec_ciber = embed_query_gemini(http, "servicio de ciberseguridad")
    v_hits = buscar_vector(vec_ciber, n=5)
    f_hits = buscar_fts("ciberseguridad", n=5)

print(f"\nVector search ({len(v_hits)} resultados):")
for h in v_hits:
    sim = h.get("similarity") or 0
    print(f"  sim={sim:.3f} id={h.get('contrato_id')} tipo={h.get('tipo')} | {(h.get('texto') or '')[:80]}")

print(f"\nFTS — buscar_contratos ({len(f_hits)} resultados):")
for h in f_hits:
    print(f"  id={h.get('id')} estado={h.get('estado')} | {(h.get('descripcion_contrato') or '')[:80]}")

v_ids = {h.get("contrato_id") for h in v_hits}
f_ids = {h.get("id") for h in f_hits}
overlap = v_ids & f_ids
print(f"\n  Overlap (IDs en ambas): {len(overlap)} — {overlap if overlap else 'ninguno'}")
print(f"  Solo vector: {v_ids - f_ids}")
print(f"  Solo FTS:    {f_ids - v_ids}")

comparacion = {
    "vector_ids": list(v_ids),
    "fts_ids": list(f_ids),
    "overlap": list(overlap),
}

output = {
    "backend": "v2",
    "queries": resultados,
    "comparacion_ciberseguridad": comparacion,
}
Path("data").mkdir(exist_ok=True)
Path("data/auditoria_rag.json").write_text(
    json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
)

print(f"\n{'='*65}")
avg_all = sum(r["avg_similarity"] for r in resultados.values()) / len(resultados)
print(f"Similitud promedio global: {avg_all:.3f}")
print("Resultados guardados en: data/auditoria_rag.json")
print("=" * 65)
