"""Reglas puras de elegibilidad y prioridad para la cola OCR."""

from __future__ import annotations

import time
from datetime import datetime, timezone

MIN_SEGUNDOS_CONTRATO = 45


def parse_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def aplanar_clasificacion(row: dict | None) -> dict | None:
    if not row:
        return row
    classification = row.get("clasificacion_contrato")
    if isinstance(classification, dict):
        row.setdefault("categoria_it", classification.get("categoria_it"))
        row.setdefault("relevancia_ia", classification.get("relevancia_ia"))
    elif isinstance(classification, list) and classification:
        row.setdefault("categoria_it", classification[0].get("categoria_it"))
        row.setdefault("relevancia_ia", classification[0].get("relevancia_ia"))
    return row


def ventana_cotizacion_abierta(
    row: dict,
    now: datetime | None = None,
    *,
    incluir_por_abrir: bool = False,
) -> bool:
    now = now or datetime.now(timezone.utc)
    end = parse_datetime(row.get("fecha_fin_cotizacion"))
    if end is None or end <= now:
        return False
    if incluir_por_abrir:
        return True
    start = parse_datetime(row.get("fecha_ini_cotizacion"))
    return start is None or start <= now


def es_ti(row: dict) -> bool:
    aplanar_clasificacion(row)
    return bool(row.get("categoria_it")) or bool(row.get("relevancia_ia"))


def prio_ti(row: dict) -> tuple:
    """Prioriza ALTA, categoría TI, MEDIA y BAJA; desempata por ID reciente."""
    aplanar_clasificacion(row)
    relevance = str(row.get("relevancia_ia") or "").strip().upper()
    category = row.get("categoria_it")
    cid = -int(row.get("id") or 0)
    if relevance == "ALTA":
        return 0, cid
    if category:
        return 1, cid
    if relevance == "MEDIA":
        return 2, cid
    if relevance == "BAJA":
        return 3, cid
    return 9, cid


def ocr_tiempo_agotado(t0: float, max_segundos: int, *, clock=time.monotonic) -> bool:
    return bool(max_segundos and max_segundos > 0 and clock() - t0 >= max_segundos)


def ocr_sin_margen_contrato(
    t0: float,
    max_segundos: int,
    *,
    min_segundos: int = MIN_SEGUNDOS_CONTRATO,
    clock=time.monotonic,
) -> bool:
    if not max_segundos or max_segundos <= 0:
        return False
    return max_segundos - (clock() - t0) < min_segundos


def filtrar_ordenar_cola_ocr(
    filas: list[dict],
    *,
    solo_ti: bool,
    exigir_ventana: bool = True,
    incluir_por_abrir: bool = False,
    now: datetime | None = None,
) -> tuple[list[dict], dict]:
    now = now or datetime.now(timezone.utc)
    stats = {
        "crudos": len(filas), "no_vigente": 0, "ventana_null": 0,
        "vencidos": 0, "por_abrir": 0, "no_ti": 0, "ok": 0,
        "alta": 0, "categoria_it": 0, "media": 0, "baja": 0,
    }
    selected: list[dict] = []
    for row in filas:
        aplanar_clasificacion(row)
        if (row.get("estado") or "Vigente") != "Vigente":
            stats["no_vigente"] += 1
            continue
        if exigir_ventana and not ventana_cotizacion_abierta(
            row, now, incluir_por_abrir=incluir_por_abrir
        ):
            end = parse_datetime(row.get("fecha_fin_cotizacion"))
            if end is None:
                stats["ventana_null"] += 1
            elif end <= now:
                stats["vencidos"] += 1
            else:
                stats["por_abrir"] += 1
            continue
        if solo_ti and not es_ti(row):
            stats["no_ti"] += 1
            continue
        selected.append(row)
        stats["ok"] += 1
        relevance = str(row.get("relevancia_ia") or "").strip().upper()
        if relevance == "ALTA":
            stats["alta"] += 1
        elif row.get("categoria_it"):
            stats["categoria_it"] += 1
        elif relevance == "MEDIA":
            stats["media"] += 1
        elif relevance == "BAJA":
            stats["baja"] += 1
    selected.sort(key=prio_ti)
    return selected, stats
