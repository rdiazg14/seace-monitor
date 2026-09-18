#!/usr/bin/env python3
"""
Fase 2 — Enriquecer contratos vigentes con datos de la API de detalle.

Para cada contrato vigente con detalle_cargado=false:
  - Llama a listar-completo?id_contrato={id}
  - Extrae: nomAreaUsuaria, lista de items CUBSO, etapas (consultas/cotizacion)
  - Actualiza contratos: nom_area_usuaria, items_json, etapas_json, detalle_cargado=true

Conexión a BD directa (psycopg/DATABASE_URL) para no heredar el statement_timeout
de 8s que impone PostgREST (rol `authenticator`): esa query de selección de
"vigentes sin detalle" se cancelaba (código 57014) bajo contención con el
REFRESH MATERIALIZED VIEW de dashboard_resumen.

Uso: uv run python enriquecer_detalle.py [--limit N] [--headed]
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

import psycopg
from playwright.sync_api import sync_playwright
from psycopg.types.json import Jsonb

# ── Cargar .env ────────────────────────────────────────────────────────────────
_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SPA_URL      = "https://prod6.seace.gob.pe/buscador-publico/contrataciones"
API_DETALLE  = ("https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico"
                "/contrataciones/listar-completo")
BATCH_DB     = 100    # contratos por lote de upsert a la BD
DELAY_S      = 0.3    # pausa entre llamadas a la API SEACE

_FMT_SEACE = "%d/%m/%Y %H:%M:%S"


def parsear_fecha(s):
    """'dd/mm/yyyy HH:MM:SS' (pared Lima) → ISO 8601 con offset -05:00.

    Mismo criterio que ingesta_completa.parsear_fecha: Perú no tiene DST.
    """
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), _FMT_SEACE).isoformat() + "-05:00"
    except Exception:
        return None


def get_vigentes_sin_detalle(conn, limit: int) -> list[dict]:
    """Vigentes sin detalle, más recientes primero (conexión directa)."""
    rows = conn.execute(
        "select id, descripcion_contrato, estado from contratos "
        "where estado = 'Vigente' and detalle_cargado = false "
        "order by id desc limit %s",
        (limit,),
    ).fetchall()
    return [
        {"id": r[0], "descripcion_contrato": r[1], "estado": r[2]}
        for r in rows
    ]


def fetch_detalle(page, contrato_id: int, retries: int = 2) -> dict | None:
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = page.request.get(
                API_DETALLE,
                params={"id_contrato": contrato_id},
                timeout=30_000,
            )
            if r.status != 200:
                last_err = f"HTTP {r.status}"
                time.sleep(1.0 * (attempt + 1))
                continue
            body = r.json()
            proj = body.get("uitContratoCompletoProjection") or {}
            items = body.get("uitContratoItemProjectionList") or []
            if not proj:
                last_err = "projection vacía"
                return None

            items_clean = [
                {
                    "cod_cubso":   i.get("codCubso"),
                    "nom_cubso":   i.get("nomCubso"),
                    "descripcion": i.get("descripcionItem"),
                    "cantidad":    i.get("cantidad"),
                    "unidad":      i.get("nomUnidadMedida"),
                    "distrito":    i.get("nomDistrito") or i.get("nomDistritoExt"),
                }
                for i in items
            ]

            # Cronograma de etapas (consultas/absoluciones, cotización, …).
            etapas = body.get("uitContratoEtapaProjectionList") or []
            etapas_clean = [
                {
                    "etapa":   e.get("nomEtapaContrato"),
                    "fec_ini": parsear_fecha(e.get("fecIni")),
                    "fec_fin": parsear_fecha(e.get("fecFin")),
                }
                for e in etapas
            ]

            tdr = (proj.get("desObjetoContrato") or "").strip() or None
            return {
                "nom_area_usuaria": proj.get("nomAreaUsuaria"),
                "descripcion":      tdr,
                "items_json":       items_clean,
                "etapas_json":      etapas_clean,
            }
        except Exception as e:
            last_err = str(e)
            print(f"    [error] fetch_detalle({contrato_id}) intento {attempt+1}: {e}", flush=True)
            time.sleep(1.0 * (attempt + 1))
    print(f"    [error] fetch_detalle({contrato_id}): {last_err}", flush=True)
    return None


def upsert_lote(conn, lote: list[dict]) -> None:
    """Upsert de filas por conexión directa (evita el statement_timeout de PostgREST)."""
    with conn.transaction():
        for fila in lote:
            items = fila.get("items_json")
            etapas = fila.get("etapas_json")
            conn.execute(
                "insert into contratos "
                "(id, nom_area_usuaria, items_json, etapas_json, detalle_cargado, descripcion) "
                "values (%s, %s, %s, %s, %s, %s) "
                "on conflict (id) do update set "
                "nom_area_usuaria = excluded.nom_area_usuaria, "
                "items_json = excluded.items_json, "
                "etapas_json = excluded.etapas_json, "
                "detalle_cargado = excluded.detalle_cargado, "
                "descripcion = coalesce(excluded.descripcion, contratos.descripcion)",
                (
                    fila["id"],
                    fila.get("nom_area_usuaria"),
                    Jsonb(items) if items is not None else None,
                    Jsonb(etapas) if etapas is not None else None,
                    True,
                    fila.get("descripcion"),
                ),
            )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit",  type=int, default=5_000,
                    help="Máximo de contratos a procesar (default: 5000)")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    if not DATABASE_URL:
        raise SystemExit("ERROR: DATABASE_URL no encontrado")

    conn = psycopg.connect(DATABASE_URL, autocommit=True)
    try:
        conn.execute("set statement_timeout = '60s'")

        print("=" * 60, flush=True)
        print("FASE 2 — Enriquecimiento de detalle", flush=True)
        print("=" * 60, flush=True)

        vigentes = get_vigentes_sin_detalle(conn, args.limit)
        total = len(vigentes)
        print(f"Contratos vigentes sin detalle: {total:,}", flush=True)
        if total == 0:
            print("Nada que hacer. ¿Ya se enriqueció todo?", flush=True)
            return

        t0        = time.time()
        ok        = 0
        errores   = 0
        pendiente = []   # lote acumulado antes de upsert

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            page    = browser.new_context(ignore_https_errors=True).new_page()

            print("Iniciando sesión SEACE...", flush=True)
            page.goto(SPA_URL, wait_until="networkidle", timeout=90_000)
            page.wait_for_timeout(2_000)
            print("Sesión lista.\n", flush=True)

            for i, contrato in enumerate(vigentes, 1):
                cid  = contrato["id"]
                desc = (contrato.get("descripcion_contrato") or "")[:50]
                detalle = fetch_detalle(page, cid)

                if detalle:
                    fila = {
                        "id":               cid,
                        "nom_area_usuaria": detalle["nom_area_usuaria"],
                        "items_json":       detalle["items_json"],
                        "etapas_json":      detalle["etapas_json"],
                        "detalle_cargado":  True,
                    }
                    if detalle.get("descripcion"):
                        fila["descripcion"] = detalle["descripcion"]
                    pendiente.append(fila)
                    ok += 1
                else:
                    # No marcar detalle_cargado: el Action diario lo reintentará
                    errores += 1

                elapsed = time.time() - t0
                rate    = i / max(elapsed, 1)
                eta     = (total - i) / rate
                print(f"  [{i}/{total}] id={cid} {'OK' if detalle else 'FAIL'} "
                      f"{desc}  {elapsed:.0f}s ~{eta:.0f}s", flush=True)

                if len(pendiente) >= BATCH_DB:
                    try:
                        upsert_lote(conn, pendiente)
                        print(f"    → upsert lote {len(pendiente)} filas OK", flush=True)
                    except Exception as e:
                        print(f"    → upsert lote FALLÓ: {e}", flush=True)
                    pendiente = []

                time.sleep(DELAY_S)

            browser.close()

        if pendiente:
            try:
                upsert_lote(conn, pendiente)
                print(f"    → upsert lote final {len(pendiente)} filas OK", flush=True)
            except Exception as e:
                print(f"    → upsert lote final FALLÓ: {e}", flush=True)

        elapsed = time.time() - t0
        print(f"\n{'='*60}", flush=True)
        print(f"Fase 2 completada en {elapsed:.0f}s", flush=True)
        print(f"  Enriquecidos : {ok:,}", flush=True)
        print(f"  Errores      : {errores:,}", flush=True)
        print(f"  Total        : {total:,}", flush=True)

        con_items = conn.execute(
            "select count(*) from contratos "
            "where detalle_cargado = true and items_json is not null"
        ).fetchone()[0]
        con_area = conn.execute(
            "select count(*) from contratos "
            "where detalle_cargado = true and nom_area_usuaria is not null"
        ).fetchone()[0]
        print(f"  Con items CUBSO     : {con_items:,}", flush=True)
        print(f"  Con nom_area_usuaria: {con_area:,}", flush=True)

        ejemplos = conn.execute(
            "select id, nro_contratacion, nom_area_usuaria, items_json "
            "from contratos where detalle_cargado = true and items_json is not null "
            "order by id desc limit 3"
        ).fetchall()
        print("\n--- 3 ejemplos items_json ---", flush=True)
        for ex in ejemplos:
            items = ex[3] or []
            print(json.dumps({
                "id": ex[0],
                "nro": ex[1],
                "area": ex[2],
                "n_items": len(items) if isinstance(items, list) else None,
                "items_json": items[:2] if isinstance(items, list) else items,
            }, ensure_ascii=False, indent=2), flush=True)
        print("=" * 60, flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
