"""Convenciones y subida tolerante del PDF original a Storage."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

BUCKET_TDR = "tdr"
MAX_PDF_STORAGE_BYTES = 52_428_800
_RUTA_ARBOL = re.compile(r"^tdr/\d{4}/\d{2}/\d+/\d+\.pdf$")
_TZ_LIMA = timezone(timedelta(hours=-5))


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


def pdf_storage_ruta(cid: int, aid: int, fecha_publica=None) -> str:
    """Construye tdr/{YYYY}/{MM}/{contrato_id}/{archivo_id}.pdf en Lima."""
    published_at = parse_datetime(fecha_publica)
    if published_at is None:
        published_at = datetime.now(timezone.utc)
    lima = published_at.astimezone(_TZ_LIMA)
    return f"tdr/{lima.year:04d}/{lima.month:02d}/{int(cid)}/{int(aid)}.pdf"


def es_ruta_arbol_tdr(path: str | None) -> bool:
    return bool(path and _RUTA_ARBOL.match(str(path).strip()))


def cachear_pdf_storage(
    supa,
    contrato: dict,
    meta: dict,
    tmp: Path,
    *,
    bucket: str = BUCKET_TDR,
    max_bytes: int = MAX_PDF_STORAGE_BYTES,
) -> None:
    """Sube el binario ya validado; un fallo de caché no rompe la extracción."""
    if supa is None or not tmp.exists():
        return
    cid = meta.get("id") or contrato.get("id")
    try:
        aid = meta.get("pdf_archivo_id") or contrato.get("pdf_archivo_id")
        if not aid:
            return
        raw = tmp.read_bytes()
        if not raw.lstrip().startswith(b"%PDF"):
            print(f"  [warn] storage tdr id={cid}: no es PDF, no se cachea", flush=True)
            return
        if len(raw) > max_bytes:
            print(
                f"  [warn] storage tdr id={cid}: {len(raw)} bytes > tope {max_bytes}",
                flush=True,
            )
            return
        path = pdf_storage_ruta(
            int(cid),
            int(aid),
            contrato.get("fecha_publica") or meta.get("fecha_publica"),
        )
        supa.storage.from_(bucket).upload(
            path,
            raw,
            {"content-type": "application/pdf", "upsert": "true"},
        )
        meta["pdf_storage_path"] = path
        meta["pdf_storage_bytes"] = len(raw)
        print(f"  storage tdr id={cid} OK {path} {len(raw)} bytes", flush=True)
    except Exception as error:
        print(f"  [warn] storage tdr id={cid}: {error}", flush=True)
