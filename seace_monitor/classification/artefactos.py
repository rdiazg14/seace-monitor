"""Artefactos JSON del flujo C1/C4: propuestas, consenso y conteos.

Los artefactos viven en ``data/`` (gitignored): son evidencia auditable de
cada corrida --proponer y del cruce --consenso que alimenta --aplicar.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from seace_monitor.config import RAIZ_REPO

from .contracts import CATEGORIA_NINGUNA

DATA_DIR = RAIZ_REPO / "data"
ARTEFACTO_MAX_DIAS = 7


def ruta_artefacto(ahora: datetime, *, data_dir: Path = DATA_DIR) -> Path:
    stamp = ahora.strftime("%Y%m%d-%H%M%S")
    return data_dir / f"propuestas_it_{stamp}.json"


def ruta_consenso(ahora: datetime, *, data_dir: Path = DATA_DIR) -> Path:
    stamp = ahora.strftime("%Y%m%d-%H%M%S")
    return data_dir / f"consenso_it_{stamp}.json"


def escribir_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def cargar_artefacto(ruta: Path) -> tuple[dict | None, int]:
    if not ruta.is_file():
        print(f"ERROR: no existe el artefacto {ruta}", flush=True)
        return None, 1
    payload = json.loads(ruta.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        print(f"ERROR: {ruta.name} no es un objeto JSON", flush=True)
        return None, 1
    return payload, 0


def conteos_items(items: list[dict]) -> dict[str, int]:
    c = {
        "alta_directa": 0,
        "desempate_ok": 0,
        "desempate_discrepa": 0,
        "ninguna": 0,
        "sin_respuesta": 0,
        "rechazado_previo": 0,
        "senal_no_verificada": 0,
        "senal_solo_cubso": 0,
        "senal_fuente_item": 0,
        "desempate_sin_evidencia": 0,
        "p2_senal_no_verificada": 0,
        "discrepa_intra_it": 0,
        "discrepa_es_it": 0,
        "revisar": 0,
    }
    for it in items:
        dec = it.get("decision")
        orig = it.get("origen")
        if dec == "rechazado_previo":
            c["rechazado_previo"] += 1
        elif dec == "sin_respuesta":
            c["sin_respuesta"] += 1
        elif orig == "ninguna":
            c["ninguna"] += 1
        elif orig == "alta_directa":
            c["alta_directa"] += 1
        elif orig == "desempate_ok":
            c["desempate_ok"] += 1
        elif orig == "desempate_sin_evidencia":
            c["desempate_sin_evidencia"] += 1
        elif orig == "desempate_discrepa":
            c["desempate_discrepa"] += 1
        elif orig == "discrepa_intra_it":
            c["discrepa_intra_it"] += 1
        elif orig == "discrepa_es_it":
            c["discrepa_es_it"] += 1
        if it.get("revisar"):
            c["revisar"] += 1
        p1 = it.get("p1") or {}
        cat_p1 = p1.get("categoria")
        if cat_p1 and cat_p1 != CATEGORIA_NINGUNA:
            if p1.get("senal_verificada") is False:
                c["senal_no_verificada"] += 1
            if p1.get("senal_fuente") == "cubso":
                c["senal_solo_cubso"] += 1
            if p1.get("senal_fuente") == "item":
                c["senal_fuente_item"] += 1
        p2 = it.get("p2") or {}
        if p2.get("senal_verificada") is False:
            c["p2_senal_no_verificada"] += 1
    return c
