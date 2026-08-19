#!/usr/bin/env python3
"""PASO D — 2-3 queries de detalle TDR sobre contratos con PDF en el RAG v2."""
from __future__ import annotations

import httpx

from eval_retrieval import (
    SUPABASE_KEY,
    SUPABASE_URL,
    buscar_v2_vector,
    create_client,
    embed_query_gemini,
)


def main() -> None:
    s = create_client(SUPABASE_URL, SUPABASE_KEY)
    queries = [
        {
            "id": 87164,
            "q": (
                "recepcion de documentos GRELL ventanilla de partes virtual "
                "listado de expedientes Sistema de Gestion Documental OTD"
            ),
            "pista": "ventanilla",
        },
        {
            "id": 87151,
            "q": (
                "muestreo monitoreo aluminio residual arsenico hidrato de "
                "cloral SEDA Ayacucho"
            ),
            "pista": "aluminio",
        },
        {
            "id": 87150,
            "q": (
                "CLIP DE ANEURISMA RECTO 5 MM neurocirugia Hospital III "
                "Cayetano Heredia Piura"
            ),
            "pista": "ANEURISMA",
        },
    ]
    print("queries listas", flush=True)

    http = httpx.Client(timeout=60.0)
    try:
        for item in queries:
            print("=" * 60, flush=True)
            print(f"Q cid={item['id']} {item['q'][:140]!r}", flush=True)
            vec = embed_query_gemini(http, item["q"])
            hits = buscar_v2_vector(s, vec, 8, 0.20)
            pdf_top5 = False
            id_top5 = False
            for rank, h in enumerate(hits[:5], 1):
                cid = int(h.get("contrato_id") or 0)
                fuente = h.get("fuente") or ""
                marcas: list[str] = []
                if cid == item["id"]:
                    id_top5 = True
                    marcas.append("ID")
                if fuente == "pdf":
                    marcas.append("PDF")
                    if cid == item["id"]:
                        pdf_top5 = True
                pista = item.get("pista") or ""
                if pista and pista.lower() in (h.get("texto") or "").lower():
                    marcas.append("PISTA")
                print(
                    f"  #{rank} cid={cid} idx={h.get('chunk_index')} "
                    f"fuente={fuente} tipo={h.get('tipo')} "
                    f"sim={h.get('similarity'):.3f} {' '.join(marcas)}",
                    flush=True,
                )
                if "PDF" in marcas or rank == 1:
                    blob = (h.get("texto") or "")[:220].replace("\n", " | ")
                    print(f"      {blob}", flush=True)
            print(
                f"  => contrato_en_top5={id_top5}  "
                f"pdf_del_contrato_en_top5={pdf_top5}  n_hits={len(hits)}",
                flush=True,
            )
    finally:
        http.close()


if __name__ == "__main__":
    main()
