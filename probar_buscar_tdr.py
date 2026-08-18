#!/usr/bin/env python3
"""Prueba rápida de búsqueda semántica contra buscar_tdr_v2() (Gemini)."""
from __future__ import annotations

import json
import os
import sys
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


def main():
    query = " ".join(sys.argv[1:]) or "equipo de computo core i3"
    if not GEMINI_API_KEY:
        raise SystemExit("ERROR: GEMINI_API_KEY requerido")
    supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    with httpx.Client() as http:
        vec = embed_query_gemini(http, query)
    res = supa.rpc("buscar_tdr_v2", {
        "query_embedding": vec,
        "match_count": 5,
        "filter_estado": "Vigente",
        "min_similarity": 0.20,
    }).execute()
    print(f"query: {query}")
    print(f"hits: {len(res.data or [])}")
    for i, row in enumerate(res.data or [], 1):
        texto = (row.get("texto") or "")[:280]
        print(json.dumps({
            "rank": i,
            "contrato_id": row.get("contrato_id"),
            "tipo": row.get("tipo"),
            "fuente": row.get("fuente"),
            "similarity": row.get("similarity"),
            "texto": texto,
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
