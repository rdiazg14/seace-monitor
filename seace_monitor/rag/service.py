"""Casos de uso de chunking PDF independientes del entrypoint CLI."""
from __future__ import annotations

import json
import time

from seace_monitor.logging import PASO_CHUNKING, registrar_evento, registrar_run
from seace_monitor.rag.chunking import (
    TARGET_SUBCHUNK,
    approx_tokens,
    chunks_de_pdf,
    cuerpo_chunk,
    encabezado_pdf,
)
from seace_monitor.rag.repository import (
    borrar_chunks_fuente,
    cobertura_fuentes,
    ids_con_fuente_pdf,
    insert_lote,
    max_chunk_index,
    paginar,
)

BATCH_INSERT = 200


def print_cobertura_fuentes(cov: dict) -> None:
    print("\n--- cobertura api + pdf ---", flush=True)
    for key, value in cov.items():
        print(f"  {key}={value}", flush=True)
    vigentes = cov.get("vigentes") or 0
    ambos = cov.get("contratos_api_y_pdf") or 0
    if vigentes:
        print(
            f"  vigentes con ambas fuentes: {ambos}/{vigentes} "
            f"({100.0 * ambos / vigentes:.1f}%)",
            flush=True,
        )


def run_solo_pdf(
    supa, ids_fijos: list[int], limit: int, *, solo_nuevos: bool = False
) -> None:
    """Inserta chunks fuente=pdf. No borra ni reescribe fuente=api."""
    cols = (
        "id, nro_contratacion, descripcion_contrato, descripcion, entidad, "
        "objeto, estado, nom_area_usuaria, items_json, tdr_texto, chunk_version"
    )
    if ids_fijos:
        res = supa.table("contratos").select(cols).in_("id", ids_fijos).execute()
        by_id = {int(r["id"]): r for r in (res.data or [])}
        contratos = [by_id[i] for i in ids_fijos if i in by_id]
    else:
        contratos = paginar(
            supa, "contratos", cols,
            eq={"estado": "Vigente"},
        )
    pendientes = [c for c in contratos if (c.get("tdr_texto") or "").strip()]
    if limit:
        pendientes = pendientes[:limit]
    print(f"  contratos con tdr_texto: {len(pendientes)}", flush=True)
    if solo_nuevos and not ids_fijos:
        ids_all = [int(c["id"]) for c in pendientes]
        ya_pdf = ids_con_fuente_pdf(supa, ids_all)
        n_skip = sum(1 for c in pendientes if int(c["id"]) in ya_pdf)
        pendientes = [c for c in pendientes if int(c["id"]) not in ya_pdf]
        print(
            f"  --solo-nuevos: omitidos con pdf chunks={n_skip} "
            f"quedan={len(pendientes)}",
            flush=True,
        )
    elif solo_nuevos and ids_fijos:
        print(
            "  --solo-nuevos + --ids: se reescriben esos ids",
            flush=True,
        )
    if not pendientes:
        print("Nada que hacer (sin tdr_texto / ya chunkeados pdf).", flush=True)
        return

    ids = [int(c["id"]) for c in pendientes]
    borrar_chunks_fuente(supa, ids, "pdf")
    print("  chunks fuente=pdf previos de la muestra borrados", flush=True)

    t0 = time.time()
    buffer: list[dict] = []
    n_chunks = 0
    n_contratos = 0
    for i, c in enumerate(pendientes, 1):
        try:
            offset = max_chunk_index(supa, int(c["id"])) + 1
            chs = chunks_de_pdf(c, chunk_index_offset=offset)
        except Exception as e:
            print(f"  [error] contrato {c.get('id')}: {e}", flush=True)
            continue
        if not chs:
            print(f"  [{i}] id={c.get('id')} sin chunks pdf", flush=True)
            continue
        buffer.extend(chs)
        n_chunks += len(chs)
        n_contratos += 1
        chars = sum(len((ch.get("chunk_embed_text") or "")) for ch in chs)
        registrar_evento(
            supa,
            int(c["id"]),
            "chunked",
            n_chunks_pdf=len(chs),
            chars_tdr=chars,
            chunk_version=(c.get("chunk_version") or "500_0"),
            detalle={"target": TARGET_SUBCHUNK, "overlap": 0},
        )
        if len(pendientes) <= 40 or i % 25 == 0 or i == len(pendientes):
            print(
                f"  [{i}/{len(pendientes)}] id={c['id']} "
                f"pdf_chunks={len(chs)} offset={offset} "
                f"header={encabezado_pdf(c)}",
                flush=True,
            )
        if len(buffer) >= BATCH_INSERT:
            insert_lote(supa, buffer)
            buffer = []
    if buffer:
        insert_lote(supa, buffer)

    elapsed = time.time() - t0
    print(f"\n{'='*60}", flush=True)
    print(f"PDF chunking en {elapsed:.0f}s  contratos={n_contratos} chunks={n_chunks}", flush=True)
    registrar_run(
        supa,
        PASO_CHUNKING,
        {
            "contratos": n_contratos,
            "chunks": n_chunks,
            "elapsed_s": round(elapsed, 1),
            "target": TARGET_SUBCHUNK,
            "overlap": 0,
            "ids": ids_fijos or None,
            "solo_nuevos": solo_nuevos,
        },
    )
    if ids_fijos or solo_nuevos:
        print("=" * 60, flush=True)
        return
    print_cobertura_fuentes(cobertura_fuentes(supa))
    if pendientes:
        cid = int(pendientes[0]["id"])
        ej = (
            supa.table("chunks_tdr")
            .select("contrato_id, chunk_index, tipo, texto, fuente")
            .eq("contrato_id", cid)
            .eq("fuente", "pdf")
            .order("chunk_index")
            .limit(3)
            .execute()
        )
        print(f"--- ejemplos fuente=pdf contrato {cid} ---", flush=True)
        for row in ej.data or []:
            texto = row.get("texto") or ""
            print(json.dumps({
                "contrato_id": row["contrato_id"],
                "chunk_index": row["chunk_index"],
                "tipo": row["tipo"],
                "fuente": row.get("fuente"),
                "n_tokens_aprox": approx_tokens(texto),
                "cuerpo": cuerpo_chunk(texto)[:240],
                "texto": texto[:400],
            }, ensure_ascii=False, indent=2), flush=True)
    print("=" * 60, flush=True)
