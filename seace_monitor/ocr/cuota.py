"""Cuota diaria de requests OCR (Flash).

Fuente de verdad: la tabla ``pipeline_cuota_ocr`` (compartida con otros
procesos). El archivo ``data/flash_ocr_cuota.json`` es un respaldo local para
auditoría vía Git y para seguir operando si la tabla no responde. Cada request
exitoso también deja trazabilidad en ``uso_ia``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from seace_monitor.config import RAIZ_REPO
from seace_monitor.gemini import fecha_lima

from .gemini_provider import LAST_OCR_USAGE, OCR_ACTIVO, CupoFlash

CUOTA_OCR_TABLA = "pipeline_cuota_ocr"
CUOTA_OCR_PATH = RAIZ_REPO / "data" / "flash_ocr_cuota.json"


def cargar_cuota_ocr(
    supa=None,
    *,
    hoy: str | None = None,
    path: Path | None = None,
) -> dict:
    """Cuota Flash OCR del día Lima.

    Fuente de verdad: BD (pipeline_cuota_ocr); fallback al archivo local si no
    hay cliente o la tabla no responde.
    """
    hoy = hoy or fecha_lima()
    path = path or CUOTA_OCR_PATH
    if supa is not None:
        try:
            res = (
                supa.table(CUOTA_OCR_TABLA)
                .select("*")
                .eq("fecha_lima", hoy)
                .maybe_single()
                .execute()
            )
            # supabase-py 2.x: maybe_single() devuelve None (no .data) sin fila.
            row = getattr(res, "data", None)
            if row:
                return {
                    "fecha": hoy,
                    "requests": int(row.get("requests") or 0),
                    "prompt_tokens": int(row.get("prompt_tokens") or 0),
                    "out_tokens": int(row.get("out_tokens") or 0),
                    "usd_est": float(row.get("usd_est") or 0.0),
                }
        except Exception as e:
            print(f"  [warn] cargar_cuota_ocr BD: {e}", flush=True)
    if path.exists():
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            if d.get("fecha") == hoy:
                d.setdefault("requests", 0)
                d.setdefault("prompt_tokens", 0)
                d.setdefault("out_tokens", 0)
                d.setdefault("usd_est", 0.0)
                return d
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "fecha": hoy,
        "requests": 0,
        "prompt_tokens": 0,
        "out_tokens": 0,
        "usd_est": 0.0,
    }


def guardar_cuota_ocr(
    supa,
    d: dict,
    *,
    path: Path | None = None,
    ahora: datetime | None = None,
) -> None:
    path = path or CUOTA_OCR_PATH
    if supa is not None:
        try:
            supa.table(CUOTA_OCR_TABLA).upsert(
                {
                    "fecha_lima": d["fecha"],
                    "requests": int(d.get("requests") or 0),
                    "prompt_tokens": int(d.get("prompt_tokens") or 0),
                    "out_tokens": int(d.get("out_tokens") or 0),
                    "usd_est": float(d.get("usd_est") or 0.0),
                    "updated_at": (
                        ahora or datetime.now(timezone.utc)
                    ).isoformat(),
                },
                on_conflict="fecha_lima",
            ).execute()
        except Exception as e:
            print(f"  [warn] guardar_cuota_ocr BD: {e}", flush=True)
    # Respaldo local (auditoría en git vía el diario). No es la fuente de verdad.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(d, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def registrar_ocr_ok(
    supa,
    cuota: dict,
    max_dia: int,
    *,
    last_usage: dict | None = None,
    path: Path | None = None,
) -> None:
    """Contabiliza un request OCR exitoso y lanza CupoFlash al tope diario."""
    usage = LAST_OCR_USAGE if last_usage is None else last_usage
    prompt = int(usage.get("prompt") or 0)
    out = int(usage.get("candidates") or 0)
    page_usd = (
        prompt / 1_000_000.0 * float(OCR_ACTIVO["usd_in"])
        + out / 1_000_000.0 * float(OCR_ACTIVO["usd_out"])
    )
    cuota["requests"] = int(cuota.get("requests") or 0) + 1
    cuota["prompt_tokens"] = int(cuota.get("prompt_tokens") or 0) + prompt
    cuota["out_tokens"] = int(cuota.get("out_tokens") or 0) + out
    cuota["usd_est"] = float(cuota.get("usd_est") or 0) + page_usd
    guardar_cuota_ocr(supa, cuota, path=path)
    if supa is not None:
        try:
            supa.table("uso_ia").insert({
                "componente": "ocr",
                "modelo": OCR_ACTIVO["modelo"],
                "tokens_prompt": prompt,
                "tokens_completion": out,
                "tokens_total": prompt + out,
                "costo_usd": page_usd,
                "cache_hit": False,
                "detalle": {
                    "version_config": OCR_ACTIVO.get("version_config") or None,
                },
            }).execute()
        except Exception as e:
            print(f"  [warn] log_uso_ia OCR: {e}", flush=True)
    if int(cuota["requests"]) >= max_dia:
        raise CupoFlash(
            f"tope diario {max_dia} Flash (usadas={cuota['requests']})",
            motivo="cupo",
        )
