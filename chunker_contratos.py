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
from seace_monitor.supabase_client import crear_cliente

import argparse
import json
import time

from seace_monitor.logging import PASO_CHUNKING, registrar_evento, registrar_run

cargar_env()

PAGE = 1_000
BATCH_INSERT = 200


def paginar(supa, tabla: str, cols: str, **filters) -> list[dict]:
    out: list[dict] = []
    offset = 0
    qbase = supa.table(tabla).select(cols)
    for k, v in filters.items():
        if k == "eq":
            for col, val in v.items():
                qbase = qbase.eq(col, val)
    while True:
        q = supa.table(tabla).select(cols)
        for k, v in filters.items():
            if k == "eq":
                for col, val in v.items():
                    q = q.eq(col, val)
        # PostgREST: .range() sin .order() no garantiza orden entre paginas;
        # la pagina 2 puede repetir filas de la 1 y omitir otras.
        res = q.order("id").range(offset, offset + PAGE - 1).execute()
        batch = res.data or []
        out.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return out


def cobertura_fuentes(supa) -> dict:
    """Cuántos vigentes tienen chunks api y/o pdf conviviendo."""
    vigentes = paginar(
        supa, "contratos", "id,tdr_texto", eq={"estado": "Vigente"},
    )
    con_tdr = {int(c["id"]) for c in vigentes if (c.get("tdr_texto") or "").strip()}
    ids = [int(c["id"]) for c in vigentes]
    api_ids: set[int] = set()
    pdf_ids: set[int] = set()
    n_pdf = n_api = 0
    n_pdf_v2 = n_api_v2 = 0
    for i in range(0, len(ids), 80):
        lote = ids[i:i + 80]
        offset = 0
        while True:
            res = (
                supa.table("chunks_tdr")
                .select("contrato_id,fuente")
                .in_("contrato_id", lote)
                # PostgREST: .range() sin .order() no garantiza orden entre paginas;
                # la pagina 2 puede repetir filas de la 1 y omitir otras.
                .order("id")
                .range(offset, offset + PAGE - 1)
                .execute()
            )
            batch = res.data or []
            for row in batch:
                cid = int(row["contrato_id"])
                if (row.get("fuente") or "api") == "pdf":
                    pdf_ids.add(cid)
                    n_pdf += 1
                else:
                    api_ids.add(cid)
                    n_api += 1
            if len(batch) < PAGE:
                break
            offset += PAGE
        pdf_v2 = (
            supa.table("chunks_tdr")
            .select("id", count="exact", head=True)
            .in_("contrato_id", lote)
            .eq("fuente", "pdf")
            .not_.is_("embedding_v2", "null")
            .execute()
        )
        api_v2 = (
            supa.table("chunks_tdr")
            .select("id", count="exact", head=True)
            .in_("contrato_id", lote)
            .eq("fuente", "api")
            .not_.is_("embedding_v2", "null")
            .execute()
        )
        n_pdf_v2 += pdf_v2.count or 0
        n_api_v2 += api_v2.count or 0
    return {
        "vigentes": len(vigentes),
        "con_tdr_texto": len(con_tdr),
        "contratos_chunk_api": len(api_ids),
        "contratos_chunk_pdf": len(pdf_ids),
        "contratos_api_y_pdf": len(api_ids & pdf_ids),
        "tdr_sin_chunk_pdf": len(con_tdr - pdf_ids),
        "chunks_api": n_api,
        "chunks_pdf": n_pdf,
        "chunks_api_v2": n_api_v2,
        "chunks_pdf_v2": n_pdf_v2,
    }


def print_cobertura_fuentes(cov: dict) -> None:
    print("\n--- cobertura api + pdf ---", flush=True)
    for k, v in cov.items():
        print(f"  {k}={v}", flush=True)
    n = cov.get("vigentes") or 0
    ambos = cov.get("contratos_api_y_pdf") or 0
    if n:
        print(
            f"  vigentes con ambas fuentes: {ambos}/{n} "
            f"({100.0 * ambos / n:.1f}%)",
            flush=True,
        )


def ids_ya_chunkeados(supa) -> set[int]:
    ids: set[int] = set()
    offset = 0
    while True:
        res = (
            supa.table("chunks_tdr")
            .select("contrato_id")
            # PostgREST: .range() sin .order() no garantiza orden entre paginas;
            # la pagina 2 puede repetir filas de la 1 y omitir otras.
            .order("id")
            .range(offset, offset + PAGE - 1)
            .execute()
        )
        batch = res.data or []
        for row in batch:
            ids.add(int(row["contrato_id"]))
        if len(batch) < PAGE:
            break
        offset += PAGE
    return ids


def insert_lote(supa, lote: list[dict]):
    try:
        supa.table("chunks_tdr").upsert(
            lote, on_conflict="contrato_id,chunk_index"
        ).execute()
    except Exception as e:
        msg = str(e).lower()
        if (
            "meta_entidad" in msg
            or "meta_nro" in msg
            or "chunk_embed_text" in msg
            or "pgrst204" in msg
        ):
            print(
                "  [aviso] columna meta/chunk_embed_text ausente; "
                "inserto sin esos campos.",
                flush=True,
            )
            stripped = [
                {
                    k: v
                    for k, v in row.items()
                    if k not in ("meta_entidad", "meta_nro", "chunk_embed_text")
                }
                for row in lote
            ]
            supa.table("chunks_tdr").upsert(
                stripped, on_conflict="contrato_id,chunk_index"
            ).execute()
        else:
            raise


def ids_con_fuente_pdf(supa, ids: list[int]) -> set[int]:
    """Contratos de `ids` que ya tienen al menos un chunk fuente=pdf."""
    found: set[int] = set()
    if not ids:
        return found
    for i in range(0, len(ids), 80):
        lote = [int(x) for x in ids[i:i + 80]]
        offset = 0
        lote_found: set[int] = set()
        while True:
            res = (
                supa.table("chunks_tdr")
                .select("contrato_id")
                .in_("contrato_id", lote)
                .eq("fuente", "pdf")
                # PostgREST: .range() sin .order() no garantiza orden entre paginas;
                # la pagina 2 puede repetir filas de la 1 y omitir otras.
                .order("id")
                .range(offset, offset + PAGE - 1)
                .execute()
            )
            batch = res.data or []
            for row in batch:
                lote_found.add(int(row["contrato_id"]))
            if len(batch) < PAGE or len(lote_found) >= len(lote):
                break
            offset += PAGE
        found |= lote_found
    return found


def borrar_chunks_fuente(supa, ids: list[int], fuente: str) -> None:
    """Borra solo chunks de una fuente. No toca las demás."""
    for i in range(0, len(ids), 80):
        lote = ids[i:i + 80]
        (
            supa.table("chunks_tdr")
            .delete()
            .in_("contrato_id", lote)
            .eq("fuente", fuente)
            .execute()
        )


def max_chunk_index(supa, contrato_id: int) -> int:
    res = (
        supa.table("chunks_tdr")
        .select("chunk_index")
        .eq("contrato_id", contrato_id)
        .order("chunk_index", desc=True)
        .limit(1)
        .execute()
    )
    if not res.data:
        return -1
    return int(res.data[0]["chunk_index"])


def borrar_chunks_vigentes(supa, ids: list[int]):
    for i in range(0, len(ids), 80):
        lote = ids[i:i + 80]
        supa.table("chunks_tdr").delete().in_("contrato_id", lote).execute()


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
