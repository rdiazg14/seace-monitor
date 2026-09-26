"""Selección del universo a clasificar: contratos sin fila en
clasificacion_contrato (cascada) más el filtro de vigencia/ventana."""

from __future__ import annotations

from datetime import datetime, timezone

from .rules import pasa_filtro

COLS = (
    "id,descripcion,descripcion_contrato,objeto,entidad,"
    "nom_area_usuaria,items_json,estado,fecha_fin_cotizacion"
)
PAGE_DB = 1_000


def paginar_nulls(
    supa,
    filtro: str,
    limit: int,
    *,
    incluir_ventana_cerrada: bool = False,
) -> list[dict]:
    """Sin fila en clasificacion_contrato + filtro de universo."""
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    offset = 0
    select = f"{COLS},clasificacion_contrato(contrato_id,categoria_it,relevancia_ia)"
    while True:
        q = (
            supa.table("contratos")
            .select(select)
            .is_("clasificacion_contrato", "null")
            .order("id", desc=True)
        )
        if filtro == "vigentes":
            q = q.eq("estado", "Vigente")
        elif filtro == "evaluacion":
            q = q.in_("estado", ["Vigente", "En Evaluación", "En Evaluacion"])
        res = q.range(offset, offset + PAGE_DB - 1).execute()
        batch = res.data or []
        for row in batch:
            if not pasa_filtro(
                row, filtro, now,
                incluir_ventana_cerrada=incluir_ventana_cerrada,
            ):
                continue
            out.append(row)
            if limit and len(out) >= limit:
                break
        print(f"  leidos {len(out):,}", flush=True)
        if len(batch) < PAGE_DB:
            break
        if limit and len(out) >= limit:
            break
        offset += PAGE_DB
    return out[:limit] if limit else out
