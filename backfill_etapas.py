#!/usr/bin/env python3
"""
Backfill de etapas_json (cronograma: consultas/absoluciones, cotización, …).

Solo el universo de Ruta del día: Vigente + En Evaluación con
categoria_it/relevancia_ia. NO toca items_json ni detalle_cargado, para
no reintroducir items_desync ni reencolar el enriquecedor diario.

Uso: uv run python backfill_etapas.py [--limit N] [--headed]
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright
from supabase import create_client
from seace_monitor.seace_api import API_DETALLE, SPA_URL, parsear_fecha

_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
DELAY_S = 0.3
PAGE_DB = 1000


def get_universo(supa) -> list[dict]:
    """Universo Ruta del día que aún no tiene etapas_json."""
    out: list[dict] = []
    offset = 0
    while True:
        res = (
            supa.table("v_contratos")
            .select("id, descripcion_contrato, estado, fecha_fin_cotizacion, etapas_json")
            .in_("estado", ["Vigente", "En Evaluación"])
            .or_("categoria_it.not.is.null,relevancia_ia.not.is.null")
            .order("id")
            .range(offset, offset + PAGE_DB - 1)
            .execute()
        )
        batch = res.data or []
        out.extend(batch)
        if len(batch) < PAGE_DB:
            break
        offset += PAGE_DB
    # Solo los que faltan; vigentes primero y, dentro, los que cierran antes.
    pendientes = [r for r in out if not r.get("etapas_json")]
    vigentes = sorted(
        (r for r in pendientes if r.get("estado") == "Vigente"),
        key=lambda r: r.get("fecha_fin_cotizacion") or "9999",
    )
    resto = sorted(
        (r for r in pendientes if r.get("estado") != "Vigente"),
        key=lambda r: r.get("fecha_fin_cotizacion") or "9999",
        reverse=True,
    )
    return vigentes + resto


def fetch_etapas(page, contrato_id: int, retries: int = 2):
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
            etapas = body.get("uitContratoEtapaProjectionList") or []
            return [
                {
                    "etapa":   e.get("nomEtapaContrato"),
                    "fec_ini": parsear_fecha(e.get("fecIni")),
                    "fec_fin": parsear_fecha(e.get("fecFin")),
                }
                for e in etapas
            ]
        except Exception as e:
            last_err = str(e)
            time.sleep(1.0 * (attempt + 1))
    print(f"    [error] fetch_etapas({contrato_id}): {last_err}", flush=True)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="Máximo de contratos (0 = todos)")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SystemExit("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY no encontrados")

    supa = create_client(SUPABASE_URL, SUPABASE_KEY)

    universo = get_universo(supa)
    if args.limit > 0:
        universo = universo[: args.limit]
    total = len(universo)
    print(f"Backfill etapas_json: {total:,} pendientes", flush=True)
    if total == 0:
        print("Nada que hacer.", flush=True)
        return

    t0 = time.time()
    ok = 0
    errores = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(SPA_URL, wait_until="networkidle", timeout=90_000)
        page.wait_for_timeout(2_000)
        print("Sesión SEACE lista.\n", flush=True)

        for i, contrato in enumerate(universo, 1):
            cid = contrato["id"]
            desc = (contrato.get("descripcion_contrato") or "")[:40]
            etapas = fetch_etapas(page, cid)

            if etapas is not None:
                try:
                    res = (
                        supa.table("contratos")
                        .update({"etapas_json": etapas})
                        .eq("id", cid)
                        .execute()
                    )
                    if res.data is None:
                        raise RuntimeError("upsert sin fila")
                    ok += 1
                except Exception as e:
                    print(f"    → upsert {cid} FALLÓ: {e}", flush=True)
                    errores += 1
            else:
                errores += 1

            elapsed = time.time() - t0
            rate = i / max(elapsed, 1)
            eta = (total - i) / rate if rate > 0 else 0
            print(f"  [{i}/{total}] id={cid} {'OK' if etapas is not None else 'FAIL'} "
                  f"{desc}  {elapsed:.0f}s ~{eta:.0f}s", flush=True)

            time.sleep(DELAY_S)

        browser.close()

    elapsed = time.time() - t0
    print(f"\n{'='*60}", flush=True)
    print(f"Backfill etapas completado en {elapsed:.0f}s", flush=True)
    print(f"  OK      : {ok:,}", flush=True)
    print(f"  Errores : {errores:,}", flush=True)


if __name__ == "__main__":
    main()
