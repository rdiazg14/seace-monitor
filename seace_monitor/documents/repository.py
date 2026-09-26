"""Acceso a contratos y persistencia documental mediante el cliente Supabase."""

from __future__ import annotations

from seace_monitor.documents.pdf_extraction import chars_utiles
from seace_monitor.ocr.queue import aplanar_clasificacion

PAGE_DB = 1_000
COLS_EXTRACCION = (
    "tdr_tipo_extraccion",
    "paginas_ocr_pendientes",
    "paginas_ocr_hechas",
    "tdr_n_paginas",
    "tdr_n_paginas_nativas",
    "tdr_n_paginas_ocr",
)
_warned_extraction = False


def contratos_por_ids(supa, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    response = (
        supa.table("contratos")
        .select(
            "id,nro_contratacion,descripcion_contrato,entidad,"
            "fecha_publica,pdf_descargado,req_url"
        )
        .in_("id", ids)
        .execute()
    )
    by_id = {int(row["id"]): row for row in (response.data or [])}
    missing = [cid for cid in ids if cid not in by_id]
    if missing:
        print(f"  [warn] ids no encontrados: {missing}", flush=True)
    return [by_id[cid] for cid in ids if cid in by_id]


def pendientes_pdf(supa, limit: int, modo: str = "todos") -> list[dict]:
    """Obtiene vigentes pendientes preservando el orden y filtros históricos."""
    if limit <= 0:
        limit = 10**9
    retry_without_pdf = modo == "sin_pdf"
    output: list[dict] = []
    offset = 0
    while len(output) < limit:
        take = min(PAGE_DB, limit - len(output))
        query = (
            supa.table("contratos")
            .select(
                "id,nro_contratacion,descripcion_contrato,entidad,"
                "fecha_publica,pdf_descargado,req_url,pdf_es_imagen"
            )
            .eq("estado", "Vigente")
        )
        if retry_without_pdf:
            query = query.or_(
                "pdf_descargado.eq.false,pdf_descargado.is.null,req_url.eq.sin_pdf"
            )
        else:
            query = query.or_("pdf_descargado.eq.false,pdf_descargado.is.null")
        response = (
            query.order("fecha_publica", desc=True, nullsfirst=False)
            .order("id", desc=True)
            .range(offset, offset + take - 1)
            .execute()
        )
        batch = response.data or []
        if modo == "ocr":
            batch = [row for row in batch if row.get("req_url") == "pendiente_ocr"]
        output.extend(batch)
        if len(response.data or []) < take:
            break
        offset += take
    return output[:limit]


def contenedores_por_ids(supa, ids: list[int]) -> list[dict]:
    """Obtiene contratos explícitos para el comando de contenedores."""
    output: list[dict] = []
    for index in range(0, len(ids), 80):
        batch_ids = ids[index:index + 80]
        response = (
            supa.table("contratos")
            .select(
                "id,nro_contratacion,descripcion_contrato,entidad,"
                "fecha_publica,pdf_descargado,req_url,tdr_texto,"
                "clasificacion_contrato(categoria_it,relevancia_ia)"
            )
            .in_("id", batch_ids)
            .execute()
        )
        output.extend(
            aplanar_clasificacion(row) for row in (response.data or [])
        )
    return output


def vigentes_ti_sin_tdr(supa, limit: int) -> list[dict]:
    """Pagina contratos vigentes TI/IA que todavía no tienen TDR."""
    output: list[dict] = []
    offset = 0
    while len(output) < limit:
        take = min(PAGE_DB, limit - len(output))
        response = (
            supa.table("contratos")
            .select(
                "id,nro_contratacion,descripcion_contrato,entidad,"
                "fecha_publica,pdf_descargado,req_url,tdr_texto,"
                "clasificacion_contrato(categoria_it,relevancia_ia)"
            )
            .eq("estado", "Vigente")
            .is_("tdr_texto", "null")
            .order("id", desc=True)
            .range(offset, offset + take - 1)
            .execute()
        )
        batch = response.data or []
        for row in batch:
            aplanar_clasificacion(row)
            if row.get("categoria_it") or row.get("relevancia_ia"):
                output.append(row)
        if len(batch) < take:
            break
        offset += take
    return output[:limit]


def guardar_texto_contenedor(
    supa,
    contrato_id: int,
    texto: str,
    meta: dict,
    *,
    min_chars_util: int,
) -> None:
    """Persiste el resultado documental de un contenedor procesable."""
    supa.table("contratos").update({
        "tdr_texto": texto or None,
        "pdf_hash": meta.get("hash"),
        "pdf_es_imagen": False,
        "pdf_descargado": True,
        "pdf_procesado": bool(
            texto and chars_utiles(texto) >= min_chars_util
        ),
        "req_url": (meta.get("url") or "")[:2000],
        "tdr_tipo_extraccion": meta.get("tipo_extraccion"),
        "paginas_ocr_pendientes": [],
        "paginas_ocr_hechas": [],
        "tdr_n_paginas": None,
        "tdr_n_paginas_nativas": None,
        "tdr_n_paginas_ocr": None,
        "pdf_archivo_id": meta.get("aid"),
        "pdf_nombre": (meta.get("nombre") or "")[:500] or None,
    }).eq("id", contrato_id).execute()


def update_contrato(supa, cid: int, payload: dict) -> None:
    """Actualiza un contrato con fallback para esquemas anteriores."""
    global _warned_extraction
    source = dict(payload)
    file_id = source.pop("_pdf_archivo_id", None)
    file_name = source.pop("_pdf_nombre", None)
    full = dict(source)
    if file_id is not None:
        full["pdf_archivo_id"] = file_id
    if file_name is not None:
        full["pdf_nombre"] = file_name
    try:
        supa.table("contratos").update(full).eq("id", cid).execute()
    except Exception as error:
        message = str(error).lower()
        if any(column in message for column in COLS_EXTRACCION):
            if not _warned_extraction:
                print(
                    "  [warn] faltan columnas de extracción; ejecuta "
                    "tdr_extraccion_meta.sql y luego --sync-meta "
                    "(meta local en data/tdr_extraccion.jsonl)",
                    flush=True,
                )
                _warned_extraction = True
            slim = {key: value for key, value in full.items() if key not in COLS_EXTRACCION}
            supa.table("contratos").update(slim).eq("id", cid).execute()
            return
        if "pdf_archivo_id" in message or "pdf_nombre" in message:
            print(
                "  [warn] faltan columnas pdf_archivo_id/pdf_nombre; "
                "ejecuta pdf_archivo_meta.sql",
                flush=True,
            )
            slim = {key: value for key, value in source.items() if key not in COLS_EXTRACCION}
            supa.table("contratos").update(slim).eq("id", cid).execute()
            return
        raise
