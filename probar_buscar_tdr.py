#!/usr/bin/env python3
"""Prueba rápida de búsqueda semántica contra buscar_tdr()."""
from __future__ import annotations

import json
import os
import sys
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

EMBED_URL = os.environ.get(
    "EMBED_URL",
    "https://seace-ai-proxy.rdiazg14.workers.dev/embed",
)


def embed(text: str) -> list[float]:
    r = httpx.post(EMBED_URL, json={"texts": [text]}, timeout=60.0)
    r.raise_for_status()
    return r.json()["embeddings"][0]


def main():
    query = " ".join(sys.argv[1:]) or "equipo de computo core i3"
    supa = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    vec = embed(query)
    res = supa.rpc("buscar_tdr", {
        "query_embedding": vec,
        "match_count": 5,
    }).execute()
    print(f"query: {query}")
    print(f"hits: {len(res.data or [])}")
    for i, row in enumerate(res.data or [], 1):
        texto = (row.get("texto") or "")[:280]
        print(json.dumps({
            "rank": i,
            "contrato_id": row.get("contrato_id"),
            "tipo": row.get("tipo"),
            "similarity": row.get("similarity"),
            "texto": texto,
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
