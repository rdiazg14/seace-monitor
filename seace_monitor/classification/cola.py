"""Cola de revisión humana: ``data/revisar_categoria.json`` + tabla
``clasificacion_pendiente`` (semilla de C3).

Los artefactos están en .gitignore; sin esta persistencia la cola se pierde
entre corridas. Nunca pisa estados aprobada/rechazada/observacion.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .artefactos import DATA_DIR, escribir_json
from .ledger import categoria_propuesta_escritura
from .rules import recortar

COLA_PATH = DATA_DIR / "revisar_categoria.json"


def _cat_de(bloque) -> str | None:
    if isinstance(bloque, dict):
        c = bloque.get("categoria")
        return c if isinstance(c, str) else None
    if isinstance(bloque, str) and bloque.strip():
        return bloque
    return None


def persistir_cola_revision(
    items: list[dict],
    artefacto: Path,
    ahora: datetime,
    *,
    path: Path = COLA_PATH,
    supa=None,
) -> None:
    # Los artefactos estan en .gitignore; sin esto la cola de revision se
    # pierde entre corridas. Esta es la semilla de la tabla
    # clasificacion_pendiente de C3.
    existentes: list[dict] = []
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("items"), list):
            existentes = [e for e in raw["items"] if isinstance(e, dict)]
    por_id: dict[int, dict] = {}
    for e in existentes:
        try:
            por_id[int(e["id"])] = e
        except (TypeError, ValueError, KeyError):
            continue
    for it in items:
        if not (it.get("revisar") or it.get("decision") == "cola"):
            continue
        cid = int(it["id"])
        prev = por_id.get(cid)
        if prev is not None and prev.get("estado") != "pendiente":
            continue
        p1 = it.get("p1") or {}
        p2 = it.get("p2") or {}
        entrada = {
            "id": cid,
            "origen": it.get("origen"),
            "p1": p1.get("categoria"),
            "p2": p2.get("categoria"),
            "escrita": categoria_propuesta_escritura(it),
            "titulo": recortar(it.get("descripcion"), 120),
            "artefacto": artefacto.name,
            "estado": "pendiente",
        }
        if it.get("votos"):
            entrada["votos"] = it["votos"]
        por_id[cid] = entrada
    vistos: set[int] = set()
    out: list[dict] = []
    for e in existentes:
        try:
            cid = int(e["id"])
        except (TypeError, ValueError, KeyError):
            out.append(e)
            continue
        vistos.add(cid)
        out.append(por_id.get(cid, e))
    for cid, e in por_id.items():
        if cid not in vistos:
            out.append(e)
    escribir_json(
        path,
        {"actualizado_utc": ahora.isoformat(), "items": out},
    )
    if supa is None:
        print(
            "  [aviso] clasificacion_pendiente no se escribio: falta SUPABASE_*",
            flush=True,
        )
        return
    try:
        n = upsert_cola_tabla(supa, items, artefacto, ahora)
        print(f"  clasificacion_pendiente upsert={n}", flush=True)
    except Exception as e:
        print(f"  [aviso] clasificacion_pendiente: {e}", flush=True)


def upsert_cola_tabla(supa, items: list[dict], artefacto: Path, ahora: datetime) -> int:
    """Escribe pendientes en clasificacion_pendiente. No pisa aprobada/rechazada/observacion."""
    candidatos: list[dict] = []
    ids: list[int] = []
    for it in items:
        if not (it.get("revisar") or it.get("decision") == "cola"):
            continue
        try:
            cid = int(it["id"])
        except (TypeError, ValueError, KeyError):
            continue
        ids.append(cid)
        candidatos.append(it)
    if not ids:
        return 0
    prev: dict[int, str] = {}
    for i in range(0, len(ids), 200):
        chunk = ids[i: i + 200]
        rows = (
            supa.table("clasificacion_pendiente")
            .select("contrato_id,estado")
            .in_("contrato_id", chunk)
            .execute()
            .data
            or []
        )
        for r in rows:
            prev[int(r["contrato_id"])] = str(r.get("estado") or "")
    filas: list[dict] = []
    for it in candidatos:
        cid = int(it["id"])
        if prev.get(cid) and prev[cid] != "pendiente":
            continue
        p1c = _cat_de(it.get("p1"))
        p2c = _cat_de(it.get("p2"))
        fila = {
            "contrato_id": cid,
            "categoria_p1": p1c,
            "categoria_p2": p2c,
            "origen": it.get("origen"),
            "votos": it.get("votos") if isinstance(it.get("votos"), dict) else None,
            "estado": "pendiente",
            "titulo": recortar(it.get("descripcion"), 120),
            "artefacto": artefacto.name,
            "creado_utc": ahora.isoformat(),
        }
        filas.append(fila)
    if not filas:
        return 0
    for i in range(0, len(filas), 100):
        lote = filas[i: i + 100]
        supa.table("clasificacion_pendiente").upsert(
            lote, on_conflict="contrato_id",
        ).execute()
    return len(filas)
