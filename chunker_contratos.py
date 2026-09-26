#!/usr/bin/env python3
"""
Chunking de TDR.

fuente=api: encabezado largo [ENTIDAD | asunto | Nº] (corpus general, no se toca).
fuente=pdf: header corto [SIGLAS | Nº] en `texto` (display LLM) + meta_entidad/meta_nro.
            El embed v2 usa el cuerpo sin header (--embed-mode auto/body).

Uso:
  python chunker_contratos.py [--limit N]
  python chunker_contratos.py --rechunk
  python chunker_contratos.py --solo-pdf --ids 87164,87001
  python chunker_contratos.py --solo-pdf --solo-nuevos
"""
from __future__ import annotations

from seace_monitor.config import cargar_env
from seace_monitor.rag.chunking import (
    MAX_TOKENS_ANTES_SPLIT,
    TARGET_SUBCHUNK,
    approx_tokens,
    chunks_de_contrato,
    chunks_de_pdf,
    con_contexto,
    con_contexto_pdf,
    cuerpo_chunk,
    cuerpo_sin_membrete,
    embed_text_pdf,
    encabezado,
    encabezado_pdf,
    meta_de_contrato,
    nro_contrato,
    objeto_corto,
    siglas_entidad,
    split_por_parrafos,
)
from seace_monitor.rag.repository import (
    borrar_chunks_fuente,
    borrar_chunks_vigentes,
    cobertura_fuentes,
    ids_con_fuente_pdf,
    ids_ya_chunkeados,
    insert_lote,
    max_chunk_index,
    paginar,
)
from seace_monitor.rag.service import (
    BATCH_INSERT,
    print_cobertura_fuentes,
    run_solo_pdf,
)
from seace_monitor.supabase_client import crear_cliente

import argparse
import json
import time

cargar_env()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = todos")
    ap.add_argument("--ids", default="",
                    help="Ids fijos separados por coma")
    ap.add_argument("--rechunk", action="store_true",
                    help="Borra/reinserta chunks de vigentes (no toca cerrados ni En Evaluacion)")
    ap.add_argument("--solo-pdf", action="store_true",
                    help="Solo chunks fuente=pdf de contratos con tdr_texto. No toca fuente=api.")
    ap.add_argument(
        "--solo-nuevos",
        action="store_true",
        help="Con --solo-pdf: no reescribe contratos que ya tienen chunks fuente=pdf",
    )
    args = ap.parse_args()

    ids_fijos = []
    if args.ids:
        ids_fijos = [int(x) for x in args.ids.replace(" ", "").split(",") if x]

    supa = crear_cliente()
    print("=" * 60, flush=True)
    print(
        f"Chunking TDR  (solo-pdf={args.solo_pdf} solo-nuevos={args.solo_nuevos} "
        f"rechunk={args.rechunk} ids={ids_fijos or '-'})",
        flush=True,
    )
    print("=" * 60, flush=True)

    if args.solo_nuevos and not args.solo_pdf:
        raise SystemExit("--solo-nuevos solo aplica con --solo-pdf")

    if args.solo_pdf:
        run_solo_pdf(supa, ids_fijos, args.limit, solo_nuevos=args.solo_nuevos)
        return

    print("Cargando vigentes con detalle...", flush=True)
    cols = (
        "id, nro_contratacion, descripcion_contrato, descripcion, entidad, "
        "objeto, estado, nom_area_usuaria, items_json, tdr_texto"
    )
    if ids_fijos:
        contratos = []
        for i in range(0, len(ids_fijos), 80):
            lote = ids_fijos[i:i + 80]
            res = (
                supa.table("contratos")
                .select(cols)
                .in_("id", lote)
                .execute()
            )
            by_id = {int(r["id"]): r for r in (res.data or [])}
            contratos.extend(by_id[j] for j in lote if j in by_id)
        print(f"  --ids: {len(contratos)}/{len(ids_fijos)} encontrados", flush=True)
    else:
        contratos = paginar(
            supa,
            "contratos",
            cols,
            eq={"detalle_cargado": True, "estado": "Vigente"},
        )
    if args.rechunk:
        pendientes = contratos
        print(f"  --rechunk: vigentes con detalle = {len(pendientes):,}", flush=True)
    else:
        ya = ids_ya_chunkeados(supa)
        pendientes = [c for c in contratos if int(c["id"]) not in ya]
        print(f"  Con detalle vigente : {len(contratos):,}", flush=True)
        print(f"  Ya con chunks       : {len(ya):,}", flush=True)
        print(f"  Pendientes          : {len(pendientes):,}", flush=True)
    if args.limit:
        pendientes = pendientes[: args.limit]
        print(f"  --limit {args.limit}: se procesan {len(pendientes):,}", flush=True)

    if not pendientes:
        print("Nada que hacer.", flush=True)
        return

    if args.rechunk:
        borrar_chunks_vigentes(supa, [int(c["id"]) for c in pendientes])
        print("  chunks previos de esos vigentes borrados", flush=True)

    t0 = time.time()
    buffer: list[dict] = []
    n_chunks = 0
    n_contratos = 0
    dist: dict[str, int] = {}
    token_sum = 0

    for i, c in enumerate(pendientes, 1):
        try:
            chs = chunks_de_contrato(c)
        except Exception as e:
            print(f"  [error] contrato {c.get('id')}: {e}", flush=True)
            continue
        for ch in chs:
            tok = approx_tokens(ch["texto"])
            token_sum += tok
            tipo_base = ch["tipo"].split(" (")[0]
            if tipo_base.startswith("Ítem técnico"):
                tipo_base = "Ítem técnico"
            dist[tipo_base] = dist.get(tipo_base, 0) + 1
        buffer.extend(chs)
        n_chunks += len(chs)
        n_contratos += 1

        if len(buffer) >= BATCH_INSERT:
            try:
                insert_lote(supa, buffer)
                print(f"  [{i}/{len(pendientes)}] insert {len(buffer)} chunks "
                      f"(acum {n_chunks:,})", flush=True)
            except Exception as e:
                print(f"  [error] upsert lote: {e}", flush=True)
            buffer = []

    if buffer:
        try:
            insert_lote(supa, buffer)
            print(f"  insert lote final {len(buffer)} chunks", flush=True)
        except Exception as e:
            print(f"  [error] upsert lote final: {e}", flush=True)

    elapsed = time.time() - t0
    print(f"\n{'='*60}", flush=True)
    print(f"Fase 3 completada en {elapsed:.0f}s", flush=True)
    print(f"  Contratos procesados : {n_contratos:,}", flush=True)
    print(f"  Chunks generados     : {n_chunks:,}", flush=True)
    if n_contratos:
        print(f"  Promedio chunks/contrato : {n_chunks / n_contratos:.2f}", flush=True)
    if n_chunks:
        print(f"  Promedio tokens/chunk    : {token_sum / n_chunks:.0f}", flush=True)
    print("  Distribución por sección:", flush=True)
    for k, v in sorted(dist.items(), key=lambda kv: -kv[1]):
        print(f"    {k}: {v:,}", flush=True)

    ej = (
        supa.table("chunks_tdr")
        .select("contrato_id, chunk_index, tipo, texto, fuente")
        .order("id", desc=True)
        .limit(3)
        .execute()
    )
    print("\n--- 3 ejemplos de chunks ---", flush=True)
    for row in ej.data or []:
        texto = row.get("texto") or ""
        print(json.dumps({
            "contrato_id": row["contrato_id"],
            "chunk_index": row["chunk_index"],
            "tipo": row["tipo"],
            "n_tokens_aprox": approx_tokens(texto),
            "texto": texto[:400],
        }, ensure_ascii=False, indent=2), flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    main()
