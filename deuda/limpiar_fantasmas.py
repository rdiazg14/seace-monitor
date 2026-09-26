#!/usr/bin/env python3
"""Limpia los «contratos fantasma» insertados por refrescar_estados_bloque.py.

Un fantasma = fila en contratos con descripcion Y entidad vacías y sin
nro_contratacion (el upsert de refrescar_estados_bloque solo mandaba estado +
fechas y, si el id no existía, lo INSERTABA incompleto).

Qué hace:
  1. Re-ingesta los fantasmas «Vigente» (los únicos con valor potencial):
     trae su detalle completo del SEACE (entidad, objeto, descripción, items,
     etapas) y rellena la fila.
  2. Borra los fantasmas NO vigentes (Culminado / En Evaluación), que son
     históricos y no aportan.

Es idempotente y seguro: los fantasmas no tienen filas dependientes
(analisis_contrato, chunks_tdr, contrato_items, clasificacion_contrato).

Uso:
  uv run python deuda/limpiar_fantasmas.py --dry-run
  uv run python deuda/limpiar_fantasmas.py
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb
from playwright.sync_api import sync_playwright

_env = Path(__file__).resolve().parent.parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

DSN = os.environ.get("DATABASE_URL", "")
SPA_URL = "https://prod6.seace.gob.pe/buscador-publico/contrataciones"
API_DETALLE = (
    "https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico"
    "/contrataciones/listar-completo"
)
_FMT_SEACE = "%d/%m/%Y %H:%M:%S"

SQL_FANTASMAS = """
    SELECT id, estado FROM contratos
    WHERE (descripcion IS NULL OR btrim(descripcion) = '')
      AND (entidad IS NULL OR btrim(entidad) = '')
      AND nro_contratacion IS NULL
    ORDER BY id
"""


def parsear_fecha(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), _FMT_SEACE).isoformat() + "-05:00"
    except Exception:
        return None


def fetch_detalle(page, cid: int) -> dict | None:
    r = page.request.get(API_DETALLE, params={"id_contrato": cid}, timeout=30_000)
    if r.status != 200:
        return None
    body = r.json()
    proj = body.get("uitContratoCompletoProjection") or {}
    if not proj:
        return None
    items = body.get("uitContratoItemProjectionList") or []
    items_clean = [
        {
            "cod_cubso": i.get("codCubso"),
            "nom_cubso": i.get("nomCubso"),
            "descripcion": i.get("descripcionItem"),
            "cantidad": i.get("cantidad"),
            "unidad": i.get("nomUnidadMedida"),
            "distrito": i.get("nomDistrito") or i.get("nomDistritoExt"),
        }
        for i in items
    ]
    etapas = body.get("uitContratoEtapaProjectionList") or []
    etapas_clean = [
        {
            "etapa": e.get("nomEtapaContrato"),
            "fec_ini": parsear_fecha(e.get("fecIni")),
            "fec_fin": parsear_fecha(e.get("fecFin")),
        }
        for e in etapas
    ]
    return {
        "entidad": proj.get("nomEntidad"),
        "objeto": proj.get("nomObjetoContrato"),
        "descripcion": (proj.get("desObjetoContrato") or "").strip() or None,
        "descripcion_contrato": proj.get("nroDescripcion"),
        "fecha_publica": parsear_fecha(proj.get("fecPublica")),
        "estado": proj.get("nomEstadoContrato"),
        "nom_area_usuaria": proj.get("nomAreaUsuaria"),
        "items_json": items_clean,
        "etapas_json": etapas_clean,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not DSN:
        print("ERROR: falta DATABASE_URL", flush=True)
        return 2

    conn = psycopg.connect(DSN, connect_timeout=30, sslmode="require")
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SET statement_timeout = '60s'")

    cur.execute(SQL_FANTASMAS)
    fantasmas = [(int(r[0]), r[1]) for r in cur.fetchall()]
    vigentes = [i for i, e in fantasmas if e == "Vigente"]
    borrar = [i for i, e in fantasmas if e != "Vigente"]
    print(f"fantasmas={len(fantasmas)}  vigentes={len(vigentes)}  a_borrar={len(borrar)}",
          flush=True)

    if args.dry_run:
        print("dry-run: vigentes a re-ingestar:", vigentes, flush=True)
        print("dry-run: ids a borrar (primeros 20):", borrar[:20], flush=True)
        conn.close()
        return 0

    # 1. Re-ingesta de los Vigente.
    if vigentes:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            page = b.new_context(ignore_https_errors=True).new_page()
            page.goto(SPA_URL, wait_until="networkidle", timeout=90_000)
            page.wait_for_timeout(2_000)
            for i, cid in enumerate(vigentes, 1):
                det = fetch_detalle(page, cid)
                if not det:
                    print(f"  [{i}/{len(vigentes)}] id={cid} sin detalle, se salta",
                          flush=True)
                    continue
                cur.execute(
                    """
                    UPDATE contratos SET
                        entidad = %(entidad)s,
                        objeto = %(objeto)s,
                        descripcion = %(descripcion)s,
                        descripcion_contrato = %(descripcion_contrato)s,
                        fecha_publica = %(fecha_publica)s,
                        estado = %(estado)s,
                        nom_area_usuaria = %(nom_area_usuaria)s,
                        items_json = %(items_json)s,
                        etapas_json = %(etapas_json)s,
                        detalle_cargado = true
                    WHERE id = %(id)s
                    """,
                    {
                        "id": cid,
                        "entidad": det["entidad"],
                        "objeto": det["objeto"],
                        "descripcion": det["descripcion"],
                        "descripcion_contrato": det["descripcion_contrato"],
                        "fecha_publica": det["fecha_publica"],
                        "estado": det["estado"],
                        "nom_area_usuaria": det["nom_area_usuaria"],
                        "items_json": Jsonb(det["items_json"]),
                        "etapas_json": Jsonb(det["etapas_json"]),
                    },
                )
                print(f"  [{i}/{len(vigentes)}] id={cid} re-ingestado "
                      f"estado={det['estado']} obj={det['objeto']}", flush=True)
                time.sleep(0.3)
            b.close()

    # 2. Borrado de fantasmas no vigentes (solo los que sigan incompletos).
    if borrar:
        cur.execute(
            """
            DELETE FROM contratos
            WHERE id = ANY(%s)
              AND (descripcion IS NULL OR btrim(descripcion) = '')
              AND (entidad IS NULL OR btrim(entidad) = '')
              AND nro_contratacion IS NULL
            """,
            (borrar,),
        )
        print(f"borrados no vigentes: {cur.rowcount}", flush=True)

    cur.execute(SQL_FANTASMAS)
    restantes = cur.fetchall()
    print(f"fantasmas restantes: {len(restantes)}", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
