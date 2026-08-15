#!/usr/bin/env python3
"""
Fase 2 — Enriquecer contratos vigentes con datos de la API de detalle.

Para cada contrato vigente con detalle_cargado=false:
  - Llama a listar-completo?id_contrato={id}
  - Extrae: nomAreaUsuaria, lista de items CUBSO
  - Actualiza contratos: nom_area_usuaria, items_json, detalle_cargado=true

Uso: uv run python enriquecer_detalle.py [--limit N] [--headed]
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from playwright.sync_api import sync_playwright
from supabase import create_client

# ── Cargar .env ────────────────────────────────────────────────────────────────
_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SPA_URL      = "https://prod6.seace.gob.pe/buscador-publico/contrataciones"
API_DETALLE  = ("https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico"
                "/contrataciones/listar-completo")
BATCH_DB     = 100    # contratos por lote de upsert a Supabase
DELAY_S      = 0.3   # pausa entre llamadas a la API SEACE


PAGE_DB = 1_000  # PostgREST/Supabase recorta selects a 1000 por defecto


def get_vigentes_sin_detalle(supa, limit: int) -> list[dict]:
    """Pagina de a 1000 para no chocar con el max_rows de Supabase."""
    out: list[dict] = []
    offset = 0
    while len(out) < limit:
        take = min(PAGE_DB, limit - len(out))
        res = (
            supa.table("contratos")
            .select("id, descripcion_contrato, estado")
            .eq("estado", "Vigente")
            .eq("detalle_cargado", False)
            .order("id", desc=True)
            .range(offset, offset + take - 1)
            .execute()
        )
        batch = res.data or []
        out.extend(batch)
        if len(batch) < take:
            break
        offset += take
    return out


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

            tdr = (proj.get("desObjetoContrato") or "").strip() or None
            return {
                "nom_area_usuaria": proj.get("nomAreaUsuaria"),
                "descripcion":      tdr,
                "items_json":       items_clean,
            }
        except Exception as e:
            last_err = str(e)
            print(f"    [error] fetch_detalle({contrato_id}) intento {attempt+1}: {e}", flush=True)
            time.sleep(1.0 * (attempt + 1))
    print(f"    [error] fetch_detalle({contrato_id}): {last_err}", flush=True)
    return None


def upsert_lote(supa, lote: list[dict]):
    supa.table("contratos").upsert(lote, on_conflict="id").execute()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit",  type=int, default=5_000,
                    help="Máximo de contratos a procesar (default: 5000)")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SystemExit("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY no encontrados")

    supa = create_client(SUPABASE_URL, SUPABASE_KEY)

    print("=" * 60, flush=True)
    print("FASE 2 — Enriquecimiento de detalle", flush=True)
    print("=" * 60, flush=True)

    vigentes = get_vigentes_sin_detalle(supa, args.limit)
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
                    upsert_lote(supa, pendiente)
                    print(f"    → upsert lote {len(pendiente)} filas OK", flush=True)
                except Exception as e:
                    print(f"    → upsert lote FALLÓ: {e}", flush=True)
                pendiente = []

            time.sleep(DELAY_S)

        browser.close()

    if pendiente:
        try:
            upsert_lote(supa, pendiente)
            print(f"    → upsert lote final {len(pendiente)} filas OK", flush=True)
        except Exception as e:
            print(f"    → upsert lote final FALLÓ: {e}", flush=True)

    elapsed = time.time() - t0
    print(f"\n{'='*60}", flush=True)
    print(f"Fase 2 completada en {elapsed:.0f}s", flush=True)
    print(f"  Enriquecidos : {ok:,}", flush=True)
    print(f"  Errores      : {errores:,}", flush=True)
    print(f"  Total        : {total:,}", flush=True)

    con_items = (
        supa.table("contratos")
        .select("id", count="exact")
        .eq("detalle_cargado", True)
        .not_.is_("items_json", "null")
        .limit(1)
        .execute()
    )
    con_area = (
        supa.table("contratos")
        .select("id", count="exact")
        .eq("detalle_cargado", True)
        .not_.is_("nom_area_usuaria", "null")
        .limit(1)
        .execute()
    )
    print(f"  Con items CUBSO     : {con_items.count:,}", flush=True)
    print(f"  Con nom_area_usuaria: {con_area.count:,}", flush=True)

    ejemplos = (
        supa.table("contratos")
        .select("id, nro_contratacion, nom_area_usuaria, items_json")
        .eq("detalle_cargado", True)
        .not_.is_("items_json", "null")
        .order("id", desc=True)
        .limit(3)
        .execute()
    )
    print("\n--- 3 ejemplos items_json ---", flush=True)
    for ex in ejemplos.data or []:
        items = ex.get("items_json") or []
        print(json.dumps({
            "id": ex["id"],
            "nro": ex.get("nro_contratacion"),
            "area": ex.get("nom_area_usuaria"),
            "n_items": len(items) if isinstance(items, list) else None,
            "items_json": items[:2] if isinstance(items, list) else items,
        }, ensure_ascii=False, indent=2), flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    main()
