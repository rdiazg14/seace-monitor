"""Persistencia PostgREST de embeddings.

El módulo conserva las consultas y escrituras del entrypoint productivo, pero
no crea clientes, llama proveedores ni decide la orquestación de una corrida.
"""
from __future__ import annotations

from seace_monitor.embeddings.preparation import vec_literal


PAGE = 1_000
ID_BATCH = 80
CHUNK_COLUMNS = (
    "id, contrato_id, chunk_index, tipo, texto, fuente, chunk_embed_text"
)


def reset_embedding_v2(supa, ids: list[int], fuente: str) -> int:
    """Pone embedding_v2=NULL solo en una muestra explícita."""
    if not ids or not fuente:
        raise SystemExit("ERROR: --reset-v2 exige --ids y --fuente (no masivo)")

    updated = 0
    for index in range(0, len(ids), ID_BATCH):
        lote = ids[index:index + ID_BATCH]
        response = (
            supa.table("chunks_tdr")
            .update({"embedding_v2": None})
            .in_("contrato_id", lote)
            .eq("fuente", fuente)
            .execute()
        )
        updated += len(response.data or [])
    return updated


def paginar_ids_vigentes(supa) -> list[int]:
    ids: list[int] = []
    offset = 0
    while True:
        response = (
            supa.table("contratos")
            .select("id")
            .eq("estado", "Vigente")
            .order("id")
            .range(offset, offset + PAGE - 1)
            .execute()
        )
        batch = response.data or []
        ids.extend(int(row["id"]) for row in batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return ids


def chunks_sin_embedding_v2(
    supa,
    vigente_ids: list[int],
    limit: int,
    fuente: str | None = None,
) -> list[dict]:
    """Lee chunks vigentes con embedding_v2 NULL sin re-embebidos."""
    out: list[dict] = []
    for index in range(0, len(vigente_ids), ID_BATCH):
        lote_ids = vigente_ids[index:index + ID_BATCH]
        offset = 0
        while True:
            take = PAGE if not limit else min(PAGE, limit - len(out))
            if take <= 0:
                return out
            query = (
                supa.table("chunks_tdr")
                .select(CHUNK_COLUMNS)
                .in_("contrato_id", lote_ids)
                .is_("embedding_v2", "null")
            )
            if fuente:
                query = query.eq("fuente", fuente)
            response = query.order("id").range(offset, offset + take - 1).execute()
            batch = response.data or []
            out.extend(batch)
            if len(batch) < take:
                break
            offset += take
            if limit and len(out) >= limit:
                return out[:limit]
    return out[:limit] if limit else out


def chunks_sin_v2_por_fuente(supa, fuente: str, limit: int) -> list[dict]:
    """Pagina directamente por fuente para evitar un IN de cientos de IDs."""
    out: list[dict] = []
    offset = 0
    while True:
        take = PAGE if not limit else min(PAGE, limit - len(out))
        if take <= 0:
            break
        response = (
            supa.table("chunks_tdr")
            .select(CHUNK_COLUMNS)
            .eq("fuente", fuente)
            .is_("embedding_v2", "null")
            .order("id")
            .range(offset, offset + take - 1)
            .execute()
        )
        batch = response.data or []
        out.extend(batch)
        if len(batch) < take:
            break
        offset += take
        if limit and len(out) >= limit:
            break
    return out[:limit] if limit else out


def guardar_embeddings_v2(
    supa,
    rows: list[dict],
    vectors: list[list[float]],
) -> None:
    """Actualiza embedding_v2 en un solo upsert, conservando las columnas requeridas."""
    updates = [
        {
            "id": row["id"],
            "contrato_id": row["contrato_id"],
            "chunk_index": row["chunk_index"],
            "tipo": row["tipo"],
            "texto": row["texto"],
            "embedding_v2": vec_literal(vector),
        }
        for row, vector in zip(rows, vectors)
    ]
    supa.table("chunks_tdr").upsert(updates, on_conflict="id").execute()


def cobertura_vigentes(supa) -> dict[str, int]:
    ids = paginar_ids_vigentes(supa)
    total = 0
    con_v2 = 0
    for index in range(0, len(ids), ID_BATCH):
        lote = ids[index:index + ID_BATCH]
        all_chunks = (
            supa.table("chunks_tdr")
            .select("id", count="exact")
            .in_("contrato_id", lote)
            .limit(1)
            .execute()
        )
        total += all_chunks.count or 0
        embedded = (
            supa.table("chunks_tdr")
            .select("id", count="exact")
            .in_("contrato_id", lote)
            .not_.is_("embedding_v2", "null")
            .limit(1)
            .execute()
        )
        con_v2 += embedded.count or 0
    return {
        "vigentes": len(ids),
        "chunks_vigentes": total,
        "chunks_v2": con_v2,
        "chunks_v2_null": total - con_v2,
    }


def contar_embeddings_v2(
    supa,
    contrato_ids: list[int],
    fuente: str | None = None,
) -> int:
    query = (
        supa.table("chunks_tdr")
        .select("id", count="exact")
        .in_("contrato_id", contrato_ids)
    )
    if fuente:
        query = query.eq("fuente", fuente)
    response = query.not_.is_("embedding_v2", "null").limit(1).execute()
    return response.count or 0
