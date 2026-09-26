"""Ledger de categorías rechazadas por el revisor humano (C3).

``data/clasificacion_rechazadas.json`` más las filas ``rechazada`` de la
tabla ``clasificacion_pendiente``. Si Gemini vuelve a proponer una categoría
ya rechazada para el mismo contrato, la propuesta baja a
``decision=rechazado_previo`` y no se escribe.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .artefactos import DATA_DIR
from .contracts import CATEGORIA_NINGUNA, CATEGORIAS_IT

LEDGER_PATH = DATA_DIR / "clasificacion_rechazadas.json"


def cargar_ledger(supa=None, *, path: Path = LEDGER_PATH) -> list[dict]:
    out: list[dict] = []
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        out = data if isinstance(data, list) else []
    if supa is None:
        return out
    try:
        rows = (
            supa.table("clasificacion_pendiente")
            .select("contrato_id,categoria_p1,categoria_p2,votos")
            .eq("estado", "rechazada")
            .execute()
            .data
            or []
        )
    except Exception as e:
        print(f"  [aviso] ledger C3 tabla: {e}", flush=True)
        return out
    vistos = {
        (int(e["id"]), e.get("categoria_rechazada"))
        for e in out
        if isinstance(e, dict) and e.get("id") is not None
    }
    for r in rows:
        try:
            cid = int(r["contrato_id"])
        except (TypeError, ValueError, KeyError):
            continue
        cats: set[str] = set()
        for c in (r.get("categoria_p1"), r.get("categoria_p2")):
            if isinstance(c, str) and c and c != CATEGORIA_NINGUNA:
                cats.add(c)
        votos = r.get("votos")
        if isinstance(votos, dict):
            for v in votos.values():
                if isinstance(v, str) and v and v != CATEGORIA_NINGUNA:
                    cats.add(v)
        for cat in cats:
            key = (cid, cat)
            if key in vistos:
                continue
            vistos.add(key)
            out.append({
                "id": cid,
                "categoria_rechazada": cat,
                "fecha": datetime.now(timezone.utc).date().isoformat(),
                "nota": "c3_rechazada",
            })
    return out


def ledger_por_id(ledger: list[dict]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for e in ledger:
        if not isinstance(e, dict):
            continue
        try:
            cid = int(e.get("id"))
        except (TypeError, ValueError):
            continue
        out.setdefault(cid, []).append(e)
    return out


def categoria_propuesta_escritura(item: dict) -> str | None:
    if item.get("decision") != "escribir":
        return None
    if item.get("origen") == "desempate_ok" and isinstance(item.get("p2"), dict):
        cat = item["p2"].get("categoria")
    else:
        p1 = item.get("p1") or {}
        cat = p1.get("categoria")
    if cat in CATEGORIAS_IT:
        return cat
    return None


def categoria_efectiva(item: dict) -> str:
    """Categoria que --aplicar escribiria; si no escribe, ninguna."""
    return categoria_propuesta_escritura(item) or CATEGORIA_NINGUNA


def aplicar_ledger(items: list[dict], ledger: list[dict]) -> None:
    por_id = ledger_por_id(ledger)
    for item in items:
        cid = int(item["id"])
        entradas = por_id.get(cid, [])
        item["en_ledger"] = bool(entradas)
        cat_prop = categoria_propuesta_escritura(item)
        if not cat_prop or not entradas:
            continue
        for e in entradas:
            if e.get("categoria_rechazada") == cat_prop:
                item["decision"] = "rechazado_previo"
                print(
                    f"[LEDGER] id={cid} vuelve a proponer {cat_prop} "
                    f"(rechazada el {e.get('fecha')})",
                    flush=True,
                )
                break
