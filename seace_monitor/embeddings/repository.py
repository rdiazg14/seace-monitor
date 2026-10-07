"""Persistencia PostgREST de embeddings.

El módulo conserva las consultas y escrituras del entrypoint productivo, pero
no crea clientes, llama proveedores ni decide la orquestación de una corrida.
Las funciones genéricas reciben la columna destino (``embedding_v2`` /
``embedding_v3``); los nombres ``*_v2`` se conservan como fachadas.
"""
from __future__ import annotations

from seace_monitor.embeddings.preparation import vec_literal


PAGE = 1_000
ID_BATCH = 80
CHUNK_COLUMNS = (
    "id, contrato_id, chunk_index, tipo, texto, fuente, chunk_embed_text"
)

# Columnas vectoriales de chunks_tdr: cada espacio de embeddings llena la
# suya (IA-007). Cualquier otro nombre es un error de programación.
COLUMNAS_EMBEDDING = ("embedding_v2", "embedding_v3")


def _col(col: str) -> str:
    """Valida la columna vectorial destino contra el esquema conocido."""
    if col not in COLUMNAS_EMBEDDING:
        raise ValueError(f"columna de embedding no soportada: {col!r}")
    return col


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


def chunks_sin_embedding(
    supa,
    col: str,
    vigente_ids: list[int],
    limit: int,
    fuente: str | None = None,
) -> list[dict]:
    """Lee chunks vigentes con ``col`` NULL sin re-embebidos."""
    columna = _col(col)
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
                .is_(columna, "null")
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


def chunks_sin_por_fuente(supa, col: str, fuente: str, limit: int) -> list[dict]:
    """Pagina directamente por fuente para evitar un IN de cientos de IDs."""
    columna = _col(col)
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
            .is_(columna, "null")
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


# FIX-013: la RPC homónima hace UPDATE por unnest tocando solo la columna del
# espacio — el statement más liviano posible bajo contención HNSW (service_role
# tiene statement_timeout=30 s). Si la función no existe en la base (PGRST202)
# se cae al upsert histórico de fila completa.
RPC_POR_COLUMNA = {
    "embedding_v2": "guardar_embeddings_v2",
    "embedding_v3": "guardar_embeddings_v3",
}

# Sub-lote por statement: un timeout desperdicia como máximo estos embeddings
# ya pagados, en vez de todo el lote del proveedor.
SUBLOTE_GUARDAR = 8


def _es_rpc_ausente(exc: Exception) -> bool:
    """PostgREST responde PGRST202 cuando la función no existe en el esquema."""
    if getattr(exc, "code", None) == "PGRST202":
        return True
    msg = str(exc)
    return "PGRST202" in msg or "Could not find the function" in msg


def _upsert_sub_lote(supa, columna: str, rows: list[dict], vectors) -> None:
    updates = [
        {
            "id": row["id"],
            "contrato_id": row["contrato_id"],
            "chunk_index": row["chunk_index"],
            "tipo": row["tipo"],
            "texto": row["texto"],
            columna: vec_literal(vector),
        }
        for row, vector in zip(rows, vectors)
    ]
    supa.table("chunks_tdr").upsert(updates, on_conflict="id").execute()


def guardar_embeddings(
    supa,
    col: str,
    rows: list[dict],
    vectors: list[list[float]],
) -> None:
    """Escribe ``col`` por sub-lotes vía RPC; fallback al upsert histórico."""
    columna = _col(col)
    rpc_nombre = RPC_POR_COLUMNA[columna]
    use_rpc = hasattr(supa, "rpc")
    for i in range(0, len(rows), SUBLOTE_GUARDAR):
        sub_rows = rows[i:i + SUBLOTE_GUARDAR]
        sub_vecs = vectors[i:i + SUBLOTE_GUARDAR]
        if use_rpc:
            try:
                supa.rpc(
                    rpc_nombre,
                    {
                        "ids": [int(row["id"]) for row in sub_rows],
                        "vectores": [vec_literal(v) for v in sub_vecs],
                    },
                ).execute()
                continue
            except Exception as exc:
                if not _es_rpc_ausente(exc):
                    raise
                use_rpc = False
        _upsert_sub_lote(supa, columna, sub_rows, sub_vecs)


def cobertura_columna(supa, col: str) -> dict[str, int | str]:
    """Cobertura de ``col`` sobre chunks de vigentes (misma forma por espacio)."""
    columna = _col(col)
    ids = paginar_ids_vigentes(supa)
    total = 0
    con_col = 0
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
            .not_.is_(columna, "null")
            .limit(1)
            .execute()
        )
        con_col += embedded.count or 0
    return {
        "vigentes": len(ids),
        "chunks_vigentes": total,
        "chunks_col": con_col,
        "chunks_col_null": total - con_col,
        "col": columna,
    }


def contar_embeddings(
    supa,
    col: str,
    contrato_ids: list[int],
    fuente: str | None = None,
) -> int:
    columna = _col(col)
    query = (
        supa.table("chunks_tdr")
        .select("id", count="exact")
        .in_("contrato_id", contrato_ids)
    )
    if fuente:
        query = query.eq("fuente", fuente)
    response = query.not_.is_(columna, "null").limit(1).execute()
    return response.count or 0


# ── Nombres históricos del espacio v2 (fachadas del corpus activo) ───────────

def chunks_sin_embedding_v2(
    supa,
    vigente_ids: list[int],
    limit: int,
    fuente: str | None = None,
) -> list[dict]:
    """Lee chunks vigentes con embedding_v2 NULL sin re-embebidos."""
    return chunks_sin_embedding(supa, "embedding_v2", vigente_ids, limit, fuente)


def chunks_sin_v2_por_fuente(supa, fuente: str, limit: int) -> list[dict]:
    return chunks_sin_por_fuente(supa, "embedding_v2", fuente, limit)


def guardar_embeddings_v2(
    supa,
    rows: list[dict],
    vectors: list[list[float]],
) -> None:
    """Actualiza embedding_v2 en un solo upsert, conservando las columnas requeridas."""
    guardar_embeddings(supa, "embedding_v2", rows, vectors)


def cobertura_vigentes(supa) -> dict[str, int]:
    """Cobertura v2 con las claves históricas (compat de consumidores)."""
    cov = cobertura_columna(supa, "embedding_v2")
    return {
        "vigentes": int(cov["vigentes"]),
        "chunks_vigentes": int(cov["chunks_vigentes"]),
        "chunks_v2": int(cov["chunks_col"]),
        "chunks_v2_null": int(cov["chunks_col_null"]),
    }


def contar_embeddings_v2(
    supa,
    contrato_ids: list[int],
    fuente: str | None = None,
) -> int:
    return contar_embeddings(supa, "embedding_v2", contrato_ids, fuente)
