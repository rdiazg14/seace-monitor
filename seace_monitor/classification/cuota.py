"""Cuota diaria C4 (clasificación Gemini) y conteo de tokens.

Fuente de verdad: la tabla ``pipeline_cuota_c4``. El archivo
``data/clasificacion_cuota.json`` es solo respaldo local para auditoría vía
Git y para seguir operando si la tabla no responde. La API key es una sola:
si C4 agota créditos, el OCR se queda sin TDRs — por eso el tope es chico.

El tope diario no es un efecto global: ``max_llamadas`` y ``path`` llegan
inyectados desde la composición del entrypoint.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from seace_monitor.config import RAIZ_REPO
from seace_monitor.gemini import fecha_lima

CUOTA_C4_TABLA = "pipeline_cuota_c4"
CUOTA_C4_PATH = RAIZ_REPO / "data" / "clasificacion_cuota.json"
MAX_LLAMADAS_DIA_DEFAULT = 150
EXIT_CUPO_C4 = 8


class CupoClasificacion(Exception):
    """Tope diario C4 alcanzado (exit 8). No confundir con HTTP 429."""


def stats_tokens() -> dict:
    """Contador de tokens de la corrida (usageMetadata acumulado)."""
    return {"prompt": 0, "candidates": 0, "total": 0, "llamadas": 0}


def acumular_tokens(stats: dict, body: dict) -> None:
    um = body.get("usageMetadata") or {}

    def n(key: str) -> int:
        try:
            return int(um.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    stats["prompt"] += n("promptTokenCount")
    stats["candidates"] += n("candidatesTokenCount")
    stats["total"] += n("totalTokenCount")
    stats["llamadas"] += 1


def cargar_cuota_c4(
    supa=None,
    *,
    hoy: str | None = None,
    path: Path | None = None,
) -> dict:
    """Cupo C4 del día Lima. Fuente de verdad: BD (pipeline_cuota_c4);
    fallback al archivo local si no hay cliente o la tabla no responde."""
    hoy = hoy or fecha_lima()
    path = path or CUOTA_C4_PATH
    if supa is not None:
        try:
            res = (
                supa.table(CUOTA_C4_TABLA)
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
                    "candidates_tokens": int(row.get("candidates_tokens") or 0),
                    "total_tokens": int(row.get("total_tokens") or 0),
                }
        except Exception as e:
            print(f"  [warn] cargar_cuota_c4 BD: {e}", flush=True)
    if path.exists():
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            if d.get("fecha") == hoy:
                d.setdefault("requests", 0)
                d.setdefault("prompt_tokens", 0)
                d.setdefault("candidates_tokens", 0)
                d.setdefault("total_tokens", 0)
                return d
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "fecha": hoy,
        "requests": 0,
        "prompt_tokens": 0,
        "candidates_tokens": 0,
        "total_tokens": 0,
    }


def guardar_cuota_c4(
    supa,
    d: dict,
    *,
    path: Path | None = None,
) -> None:
    path = path or CUOTA_C4_PATH
    if supa is not None:
        try:
            supa.table(CUOTA_C4_TABLA).upsert(
                {
                    "fecha_lima": d["fecha"],
                    "requests": int(d.get("requests") or 0),
                    "prompt_tokens": int(d.get("prompt_tokens") or 0),
                    "candidates_tokens": int(d.get("candidates_tokens") or 0),
                    "total_tokens": int(d.get("total_tokens") or 0),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="fecha_lima",
            ).execute()
        except Exception as e:
            print(f"  [warn] guardar_cuota_c4 BD: {e}", flush=True)
    # Respaldo local (auditoría en git vía el semanal). No es la fuente de verdad.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(d, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def assert_cuota_c4(
    supa=None,
    *,
    max_llamadas: int = MAX_LLAMADAS_DIA_DEFAULT,
    path: Path | None = None,
) -> None:
    """Antes de llamar Gemini. Exit path: CupoClasificacion → EXIT_CUPO_C4."""
    path = path or CUOTA_C4_PATH
    cuota = cargar_cuota_c4(supa, path=path)
    usados = int(cuota.get("requests") or 0)
    if usados >= max_llamadas:
        raise CupoClasificacion(
            f"tope diario C4 {max_llamadas} llamadas "
            f"(usadas={usados}, archivo={path.name})"
        )


def registrar_llamada_c4(
    supa,
    body: dict,
    *,
    max_llamadas: int = MAX_LLAMADAS_DIA_DEFAULT,
    path: Path | None = None,
) -> None:
    """Tras respuesta OK. No toca flash_ocr_cuota.json. No lanza: el tope
    se corta en assert_cuota_c4 de la siguiente llamada."""
    um = body.get("usageMetadata") or {}

    def n(key: str) -> int:
        try:
            return int(um.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    cuota = cargar_cuota_c4(supa, path=path)
    cuota["requests"] = int(cuota.get("requests") or 0) + 1
    cuota["prompt_tokens"] = int(cuota.get("prompt_tokens") or 0) + n(
        "promptTokenCount"
    )
    cuota["candidates_tokens"] = int(cuota.get("candidates_tokens") or 0) + n(
        "candidatesTokenCount"
    )
    cuota["total_tokens"] = int(cuota.get("total_tokens") or 0) + n(
        "totalTokenCount"
    )
    guardar_cuota_c4(supa, cuota, path=path)
    usados = int(cuota["requests"])
    if usados >= max_llamadas:
        print(
            f"  [cupo C4] tope {max_llamadas} alcanzado "
            f"(usadas={usados}); la siguiente llamada aborta",
            flush=True,
        )
