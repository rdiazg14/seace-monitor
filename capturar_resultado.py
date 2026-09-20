#!/usr/bin/env python3
"""
Captura el resultado final (Desierto / Adjudicado) de contratos IT culminados.

El estado 'Culminado' no distingue el desenlace: Desierto, Anulado y Adjudicado
terminan todos como idEstadoContrato=4. El desenlace real vive a nivel de ÍTEM
en el detalle (listar-completo):
  uitContratoItemProjectionList[].nomEstadoCotiza  = "DESIERTO" | ...
  uitContratoItemProjectionList[].codRuc / nomRazonSocial / precioTotal
  (proveedor ganador y monto, solo cuando hubo adjudicación).

Procesa contratos IT (categoria_it o relevancia_ia) en estado 'Culminado' con
resultado_cargado=false, priorizando los de cierre más reciente. Idempotente:
marca resultado_cargado=true tras una lectura limpia (aunque no haya resultado).

Uso:
  python capturar_resultado.py
  python capturar_resultado.py --limit 20 --dry-run
"""
from __future__ import annotations

from seace_monitor.config import cargar_env
from seace_monitor.db import connect
from seace_monitor.seace_api import API_DETALLE, SPA_URL

import argparse
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from playwright.sync_api import sync_playwright

cargar_env()

DATABASE_URL = os.environ.get("DATABASE_URL", "")
DELAY_S      = 0.3


def _ids_pendientes(conn, limit: int) -> list[int]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.id
        FROM contratos c
        JOIN clasificacion_contrato cl ON cl.contrato_id = c.id
        WHERE (cl.categoria_it IS NOT NULL OR cl.relevancia_ia IS NOT NULL)
          AND c.estado = 'Culminado'
          AND c.resultado_cargado = false
        ORDER BY c.id DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [r[0] for r in cur.fetchall()]


def _fetch_resultado(page, cid: int, retries: int = 2) -> dict | None:
    """Devuelve dict con resultado + ganador, o None si falló el fetch."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = page.request.get(
                API_DETALLE,
                params={"id_contrato": cid},
                timeout=30_000,
            )
            if r.status != 200:
                last_err = f"HTTP {r.status}"
                time.sleep(1.0 * (attempt + 1))
                continue
            body = r.json()
            items = body.get("uitContratoItemProjectionList") or []
            return _extraer_resultado(items)
        except Exception as e:
            last_err = str(e)
            time.sleep(1.0 * (attempt + 1))
    print(f"    [error] detalle({cid}): {last_err}", flush=True)
    return None


def _extraer_resultado(items: list[dict]) -> dict:
    """Normaliza el desenlace desde la lista de ítems del detalle."""
    desierto = False
    ganador: dict | None = None
    for it in items:
        estado_cotiza = (it.get("nomEstadoCotiza") or "").strip().upper()
        if estado_cotiza == "DESIERTO":
            desierto = True
        ruc = (it.get("codRuc") or "").strip()
        razon = (it.get("nomRazonSocial") or "").strip()
        if (ruc or razon) and ganador is None:
            ganador = {
                "ruc_ganador": ruc or None,
                "proveedor_ganador": razon or None,
                "monto_adjudicado": it.get("precioTotal"),
            }

    if desierto:
        return {"resultado": "DESIERTO"}
    if ganador:
        return {"resultado": "ADJUDICADO", **ganador}
    return {"resultado": None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=250,
                    help="Máximo de contratos a procesar (default 250)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Solo consulta y loguea; no escribe en BD")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    if not DATABASE_URL:
        raise SystemExit("ERROR: DATABASE_URL no encontrado")

    print("=" * 60, flush=True)
    print("Captura de resultado (Desierto/Adjudicado) — IT culminados", flush=True)
    print(f"  limit={args.limit}  dry-run={args.dry_run}", flush=True)
    print("=" * 60, flush=True)

    conn = connect(DATABASE_URL)
    holder = [conn]  # mutable para que la reconexión persista entre filas

    def _conectar() -> "psycopg.Connection":
        return connect(DATABASE_URL)

    def _escribir(fila: dict) -> None:
        """UPDATE de una fila; reconecta y reintenta si la BD cortó la conexión."""
        sql = """
            UPDATE contratos
            SET resultado = %s,
                proveedor_ganador = %s,
                ruc_ganador = %s,
                monto_adjudicado = %s,
                resultado_cargado = true
            WHERE id = %s
        """
        params = (
            fila.get("resultado"),
            fila.get("proveedor_ganador"),
            fila.get("ruc_ganador"),
            fila.get("monto_adjudicado"),
            fila["id"],
        )
        for intento in (0, 1):
            try:
                cur = holder[0].cursor()
                cur.execute(sql, params)
                holder[0].commit()
                return
            except psycopg.OperationalError as e:
                print(f"    [reconexión] BD cortó la conexión ({e}); reintentando...",
                      flush=True)
                try:
                    holder[0].close()
                except Exception:
                    pass
                holder[0] = _conectar()

    try:
        ids = _ids_pendientes(conn, args.limit)
    except psycopg.OperationalError:
        holder[0] = _conectar()
        ids = _ids_pendientes(holder[0], args.limit)

    total = len(ids)
    print(f"IT culminados sin resultado: {total:,}", flush=True)
    if total == 0:
        holder[0].close()
        print("Nada que hacer.", flush=True)
        return

    t0 = time.time()
    ok = 0
    errores = 0
    conteo: dict[str, int] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_context(ignore_https_errors=True).new_page()
        page.goto(SPA_URL, wait_until="networkidle", timeout=90_000)
        page.wait_for_timeout(2_000)

        for i, cid in enumerate(ids, 1):
            res = _fetch_resultado(page, cid)
            if res is None:
                errores += 1
            else:
                resultado = res.get("resultado") or "SIN_RESULTADO"
                conteo[resultado] = conteo.get(resultado, 0) + 1
                if not args.dry_run:
                    fila = {"id": cid, **res}
                    _escribir(fila)
                ok += 1

            if i % 25 == 0 or i == total:
                elapsed = time.time() - t0
                print(f"  [{i}/{total}] {elapsed:.0f}s  {conteo}", flush=True)
            time.sleep(DELAY_S)

        browser.close()

    holder[0].close()

    elapsed = time.time() - t0
    print(f"\n{'=' * 60}", flush=True)
    print(f"Captura completada en {elapsed:.0f}s", flush=True)
    print(f"  Procesados: {ok:,}", flush=True)
    print(f"  Errores   : {errores:,}", flush=True)
    print(f"  Resultado : {conteo}", flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    main()
