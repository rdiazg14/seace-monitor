"""Acceso a datos para los chunks de contratos.

Este módulo concentra las operaciones PostgREST que antes vivían en el
entrypoint ``chunker_contratos.py``. No crea el cliente ni decide qué contratos
procesar; recibe un cliente compatible con supabase-py para mantener el CLI y
facilitar pruebas sin servicios remotos.
"""
from __future__ import annotations


PAGE = 1_000
ID_BATCH = 80


def paginar(supa, tabla: str, cols: str, **filters) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        query = supa.table(tabla).select(cols)
        for key, values in filters.items():
            if key == "eq":
                for column, value in values.items():
                    query = query.eq(column, value)
        # PostgREST: paginar sin orden estable puede repetir u omitir filas.
        response = query.order("id").range(offset, offset + PAGE - 1).execute()
        batch = response.data or []
        out.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return out


def cobertura_fuentes(supa) -> dict:
    """Resume cuántos contratos vigentes tienen chunks API y/o PDF."""
    vigentes = paginar(
        supa,
        "contratos",
        "id,tdr_texto",
        eq={"estado": "Vigente"},
    )
    con_tdr = {
        int(contrato["id"])
        for contrato in vigentes
        if (contrato.get("tdr_texto") or "").strip()
    }
    ids = [int(contrato["id"]) for contrato in vigentes]
    api_ids: set[int] = set()
    pdf_ids: set[int] = set()
    n_pdf = n_api = 0
    n_pdf_v2 = n_api_v2 = 0

    for index in range(0, len(ids), ID_BATCH):
        lote = ids[index:index + ID_BATCH]
        offset = 0
        while True:
            response = (
                supa.table("chunks_tdr")
                .select("contrato_id,fuente")
                .in_("contrato_id", lote)
                .order("id")
                .range(offset, offset + PAGE - 1)
                .execute()
            )
            batch = response.data or []
            for row in batch:
                contrato_id = int(row["contrato_id"])
                if (row.get("fuente") or "api") == "pdf":
                    pdf_ids.add(contrato_id)
                    n_pdf += 1
                else:
                    api_ids.add(contrato_id)
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


def ids_ya_chunkeados(supa) -> set[int]:
    ids: set[int] = set()
    offset = 0
    while True:
        response = (
            supa.table("chunks_tdr")
            .select("contrato_id")
            .order("id")
            .range(offset, offset + PAGE - 1)
            .execute()
        )
        batch = response.data or []
        ids.update(int(row["contrato_id"]) for row in batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return ids


def insert_lote(supa, lote: list[dict]) -> None:
    try:
        supa.table("chunks_tdr").upsert(
            lote,
            on_conflict="contrato_id,chunk_index",
        ).execute()
    except Exception as exc:
        message = str(exc).lower()
        if not any(
            marker in message
            for marker in ("meta_entidad", "meta_nro", "chunk_embed_text", "pgrst204")
        ):
            raise

        print(
            "  [aviso] columna meta/chunk_embed_text ausente; "
            "inserto sin esos campos.",
            flush=True,
        )
        stripped = [
            {
                key: value
                for key, value in row.items()
                if key not in ("meta_entidad", "meta_nro", "chunk_embed_text")
            }
            for row in lote
        ]
        supa.table("chunks_tdr").upsert(
            stripped,
            on_conflict="contrato_id,chunk_index",
        ).execute()


def ids_con_fuente_pdf(supa, ids: list[int]) -> set[int]:
    """Devuelve los contratos que ya tienen al menos un chunk PDF."""
    found: set[int] = set()
    for index in range(0, len(ids), ID_BATCH):
        lote = [int(value) for value in ids[index:index + ID_BATCH]]
        offset = 0
        lote_found: set[int] = set()
        while True:
            response = (
                supa.table("chunks_tdr")
                .select("contrato_id")
                .in_("contrato_id", lote)
                .eq("fuente", "pdf")
                .order("id")
                .range(offset, offset + PAGE - 1)
                .execute()
            )
            batch = response.data or []
            lote_found.update(int(row["contrato_id"]) for row in batch)
            if len(batch) < PAGE or len(lote_found) >= len(lote):
                break
            offset += PAGE
        found |= lote_found
    return found


def borrar_chunks_fuente(supa, ids: list[int], fuente: str) -> None:
    """Borra chunks de una fuente sin tocar las demás."""
    for index in range(0, len(ids), ID_BATCH):
        lote = ids[index:index + ID_BATCH]
        (
            supa.table("chunks_tdr")
            .delete()
            .in_("contrato_id", lote)
            .eq("fuente", fuente)
            .execute()
        )


def max_chunk_index(supa, contrato_id: int) -> int:
    response = (
        supa.table("chunks_tdr")
        .select("chunk_index")
        .eq("contrato_id", contrato_id)
        .order("chunk_index", desc=True)
        .limit(1)
        .execute()
    )
    if not response.data:
        return -1
    return int(response.data[0]["chunk_index"])


def borrar_chunks_vigentes(supa, ids: list[int]) -> None:
    for index in range(0, len(ids), ID_BATCH):
        lote = ids[index:index + ID_BATCH]
        supa.table("chunks_tdr").delete().in_("contrato_id", lote).execute()
