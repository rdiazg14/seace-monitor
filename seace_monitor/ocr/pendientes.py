"""Construcción de la cola OCR: contratos vigentes con páginas imagen.

Combina la BD (``contratos``), el sidecar local (``meta``) y las reglas puras
de ``queue`` (ventana de cotización, prioridad TI). Idempotente: las páginas en
``paginas_ocr_hechas`` no se re-OCR y ``fecha_fin_cotizacion`` NULL no gasta
Flash.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from seace_monitor.documents.meta import (
    columnas_extraccion_ok,
    meta_local_por_id,
)
from seace_monitor.documents.repository import PAGE_DB, contratos_por_ids
from seace_monitor.ocr.queue import (
    aplanar_clasificacion,
    es_ti,
    filtrar_ordenar_cola_ocr,
    parse_datetime,
    ventana_cotizacion_abierta,
)


def _as_int_list(raw) -> list[int]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = json.loads(raw)
    out: list[int] = []
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def contrato_ocr_sigue_elegible(
    supa,
    cid: int,
    *,
    solo_ti: bool,
    incluir_por_abrir: bool = False,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """SELECT fresco: Vigente + ventana abierta (+ TI si aplica)."""
    res = (
        supa.table("contratos")
        .select(
            "id,estado,fecha_ini_cotizacion,fecha_fin_cotizacion,"
            "clasificacion_contrato(categoria_it,relevancia_ia)"
        )
        .eq("id", cid)
        .limit(1)
        .execute()
    )
    if not res.data:
        return False, "no_encontrado"
    r = aplanar_clasificacion(res.data[0])
    if (r.get("estado") or "") != "Vigente":
        return False, f"estado={r.get('estado')}"
    now = now or datetime.now(timezone.utc)
    if not ventana_cotizacion_abierta(
        r, now, incluir_por_abrir=incluir_por_abrir
    ):
        fin = parse_datetime(r.get("fecha_fin_cotizacion"))
        if fin is None:
            return False, "ventana_null"
        if fin <= now:
            return False, "vencido"
        return False, "por_abrir"
    if solo_ti and not es_ti(r):
        return False, "no_ti"
    return True, "ok"


def _enriquecer_cola_ocr(supa, filas: list[dict]) -> list[dict]:
    ids = [int(r["id"]) for r in filas]
    extra: dict[int, dict] = {}
    for i in range(0, len(ids), 80):
        lote = ids[i:i + 80]
        res = (
            supa.table("contratos")
            .select(
                "id,estado,fecha_ini_cotizacion,fecha_fin_cotizacion,"
                "clasificacion_contrato(categoria_it,relevancia_ia)"
            )
            .in_("id", lote)
            .execute()
        )
        for row in res.data or []:
            extra[int(row["id"])] = aplanar_clasificacion(row)
    for r in filas:
        e = extra.get(int(r["id"])) or {}
        for k in (
            "estado",
            "fecha_ini_cotizacion",
            "fecha_fin_cotizacion",
            "categoria_it",
            "relevancia_ia",
        ):
            if r.get(k) in (None, "") and e.get(k) not in (None, ""):
                r[k] = e.get(k)
            elif k not in r:
                r[k] = e.get(k)
    return filas


def pendientes_ocr_paginas(
    supa,
    limit: int,
    *,
    solo_ti: bool = False,
    exigir_ventana: bool = True,
    incluir_por_abrir: bool = False,
    meta_path: Path | None = None,
    now: datetime | None = None,
) -> tuple[list[dict], dict]:
    """Vigentes+ventana+TI (opcional) con páginas imagen pendientes.

    Idempotente: `paginas_ocr_hechas` no se re-OCR. NULL en fecha_fin no gasta
    Flash.
    """
    if limit <= 0:
        limit = 10**9
    local = meta_local_por_id(path=meta_path)
    cols = (
        "id,nro_contratacion,descripcion_contrato,entidad,fecha_publica,"
        "pdf_descargado,req_url,pdf_es_imagen,tdr_texto,pdf_archivo_id,pdf_nombre,"
        "estado,fecha_ini_cotizacion,fecha_fin_cotizacion,"
        "clasificacion_contrato(categoria_it,relevancia_ia)"
    )
    if columnas_extraccion_ok(supa):
        cols += (
            ",paginas_ocr_pendientes,paginas_ocr_hechas,tdr_tipo_extraccion,"
            "tdr_n_paginas,tdr_n_paginas_nativas,tdr_n_paginas_ocr"
        )
    out: list[dict] = []
    offset = 0
    while True:
        res = (
            supa.table("contratos")
            .select(cols)
            .eq("estado", "Vigente")
            .eq("pdf_descargado", True)
            .eq("pdf_es_imagen", True)
            .order("id", desc=True)
            .range(offset, offset + PAGE_DB - 1)
            .execute()
        )
        batch = res.data or []
        for r in batch:
            aplanar_clasificacion(r)
            cid = int(r["id"])
            loc = local.get(cid) or {}
            pend = _as_int_list(r.get("paginas_ocr_pendientes")) or _as_int_list(
                loc.get("paginas_ocr_pendientes")
            )
            hechas = _as_int_list(r.get("paginas_ocr_hechas")) or _as_int_list(
                loc.get("paginas_ocr_hechas")
            )
            pend = [p for p in pend if p not in hechas]
            if not pend:
                continue
            r["paginas_ocr_pendientes"] = pend
            r["paginas_ocr_hechas"] = hechas
            r["tdr_tipo_extraccion"] = (
                r.get("tdr_tipo_extraccion")
                or loc.get("tdr_tipo_extraccion")
            )
            r["tdr_n_paginas"] = r.get("tdr_n_paginas") or loc.get("tdr_n_paginas")
            r["n_paginas_nativas"] = (
                r.get("tdr_n_paginas_nativas")
                or loc.get("tdr_n_paginas_nativas")
            )
            r["n_paginas_ocr"] = (
                r.get("tdr_n_paginas_ocr")
                or loc.get("tdr_n_paginas_ocr")
                or len(pend) + len(hechas)
            )
            out.append(r)
        if len(batch) < PAGE_DB:
            break
        offset += PAGE_DB
    if not out:
        ids = [
            cid for cid, rec in local.items()
            if _as_int_list(rec.get("paginas_ocr_pendientes"))
        ]
        if not ids:
            return [], filtrar_ordenar_cola_ocr(
                [],
                solo_ti=solo_ti,
                exigir_ventana=exigir_ventana,
                incluir_por_abrir=incluir_por_abrir,
                now=now,
            )[1]
        filas = contratos_por_ids(supa, ids)
        by = {int(r["id"]): r for r in filas}
        extra = (
            supa.table("contratos")
            .select(
                "id,tdr_texto,pdf_archivo_id,pdf_nombre,pdf_es_imagen,"
                "estado,fecha_ini_cotizacion,fecha_fin_cotizacion,"
                "clasificacion_contrato(categoria_it,relevancia_ia)"
            )
            .in_("id", ids[:800])
            .execute()
        )
        extra_by = {
            int(r["id"]): aplanar_clasificacion(r) for r in (extra.data or [])
        }
        merged: list[dict] = []
        for cid in ids:
            if cid not in by and cid not in extra_by:
                continue
            row = {**(by.get(cid) or {}), **(extra_by.get(cid) or {})}
            rec = local[cid]
            pend = [
                p for p in _as_int_list(rec.get("paginas_ocr_pendientes"))
                if p not in _as_int_list(rec.get("paginas_ocr_hechas"))
            ]
            if not pend:
                continue
            row["id"] = cid
            row["paginas_ocr_pendientes"] = pend
            row["paginas_ocr_hechas"] = _as_int_list(rec.get("paginas_ocr_hechas"))
            row["tdr_tipo_extraccion"] = rec.get("tdr_tipo_extraccion")
            row["tdr_n_paginas"] = rec.get("tdr_n_paginas")
            row["n_paginas_nativas"] = rec.get("tdr_n_paginas_nativas")
            row["n_paginas_ocr"] = rec.get("tdr_n_paginas_ocr")
            merged.append(row)
        out = merged
    out = _enriquecer_cola_ocr(supa, out)
    filtradas, stats = filtrar_ordenar_cola_ocr(
        out,
        solo_ti=solo_ti,
        exigir_ventana=exigir_ventana,
        incluir_por_abrir=incluir_por_abrir,
        now=now,
    )
    return filtradas[:limit], stats
