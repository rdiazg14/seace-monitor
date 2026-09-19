#!/usr/bin/env python3
"""
Refresco de estado en bloque vía listado (buscador), sin detalle por contrato.

Reemplaza el scraping individual de G1 (refresh_estados.py): el listado ya trae
nomEstadoContrato + cotizar + fecIni/fecFinCotizacion para TODO el corpus en
páginas de 100. Una pasada completa (~778 páginas, ~5-8 min) refresca estado,
cotizar y fechas de todos los contratos, en vez de ~6000 requests al detalle.

La ingesta incremental (ingesta_completa.py) captura las altas con su flujo
completo (validación + clasificación); este script solo actualiza columnas de
estado/fechas de contratos YA EXISTENTES, idempotente. No inserta filas nuevas:
el listado trae contratos históricos que nunca pasaron por la ingesta y un
upsert los crearía como «fantasmas» sin descripcion/entidad (incompletos en el
Buscador).

Conexión a BD directa (psycopg/DATABASE_URL) para no heredar el
statement_timeout de 8s de PostgREST: el upsert de ~500 filas contra la tabla
contratos (80k+) se cancelaba (código 57014) bajo contención con el REFRESH
MATERIALIZED VIEW de dashboard_resumen.

Uso:
  python refrescar_estados_bloque.py
  python refrescar_estados_bloque.py --limit-paginas 5   # smoke test
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from playwright.sync_api import sync_playwright

_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SPA_URL      = "https://prod6.seace.gob.pe/buscador-publico/contrataciones"
API_LISTA    = ("https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico"
                "/contrataciones/buscador")
ANIO         = datetime.now().year
PAGE_SIZE    = 100
BATCH_DB     = 500
DELAY_S      = 0.2
_FMT_SEACE   = "%d/%m/%Y %H:%M:%S"


def parsear_fecha(s: str | None) -> str | None:
    if not s:
        return None
    try:
        dt = datetime.strptime(s.strip(), _FMT_SEACE)
    except Exception:
        return None
    # SEACE entrega hora de pared Lima. Un año fuera de rango es dato corrupto
    # en el origen (p. ej. fecFinCotizacion con año 2052/2206/4202 en contratos
    # legacy): devolver None evita escribir fechas futuras absurdas.
    if dt.year < 2000 or dt.year > datetime.now().year + 2:
        return None
    return dt.isoformat() + "-05:00"


def _api_call(page, page_num: int) -> tuple[int, list[dict], int]:
    r = page.request.get(
        API_LISTA,
        params={
            "anio": ANIO, "palabra_clave": "",
            "orden": 2, "page": page_num, "page_size": PAGE_SIZE,
        },
        timeout=60_000,
    )
    if r.status != 200:
        return 0, [], r.status
    j = r.json()
    return (
        j.get("pageable", {}).get("totalElements", 0),
        j.get("data", []) or [],
        200,
    )


def upsert_lote(conn, lote: list[dict]) -> int:
    """Upsert de estado/fechas por conexión directa (evita el timeout de PostgREST)."""
    if not lote:
        return 0
    with conn.transaction():
        for fila in lote:
            conn.execute(
                "insert into contratos "
                "(id, estado, cotizar, fecha_ini_cotizacion, fecha_fin_cotizacion, "
                " estado_verificado_at) "
                "values (%s, %s, %s, %s, %s, %s) "
                "on conflict (id) do update set "
                "estado = excluded.estado, "
                "cotizar = excluded.cotizar, "
                "fecha_ini_cotizacion = coalesce(excluded.fecha_ini_cotizacion, contratos.fecha_ini_cotizacion), "
                "fecha_fin_cotizacion = coalesce(excluded.fecha_fin_cotizacion, contratos.fecha_fin_cotizacion), "
                "estado_verificado_at = excluded.estado_verificado_at",
                (
                    fila["id"],
                    fila["estado"],
                    fila["cotizar"],
                    fila["fecha_ini_cotizacion"],
                    fila["fecha_fin_cotizacion"],
                    fila["estado_verificado_at"],
                ),
            )
    return len(lote)


def cargar_ids_existentes(conn) -> set[int]:
    """Ids que ya existen en contratos (los únicos que se refrescan aquí).

    El listado trae contratos históricos que nunca pasaron por la ingesta
    (id < MAX y fuera del backfill original). Hacerles upsert los insertaría
    como «fantasmas» con solo estado/fechas y sin descripcion/entidad, que
    luego aparecen incompletos en el Buscador. Por eso este paso actualiza
    SOLO ids existentes; las altas las cubre ingesta_completa.py.
    """
    rows = conn.execute("select id from contratos").fetchall()
    return {int(r[0]) for r in rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-paginas", type=int, default=0,
                    help="Tope de páginas (0 = todo el listado). Para smoke test.")
    ap.add_argument("--max-segundos", type=int, default=0,
                    help=(
                        "Tope de reloj en segundos (0 = sin tope). Al alcanzarlo "
                        "corta y lo restante corre en la próxima pasada. El "
                        "listado viene por id descendente (vigentes/nuevos "
                        "primero), así que cortar no pierde los importantes."
                    ))
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    if not DATABASE_URL:
        raise SystemExit("ERROR: DATABASE_URL no encontrado")

    conn = psycopg.connect(DATABASE_URL, autocommit=True)
    try:
        conn.execute("set statement_timeout = '60s'")
        now_iso = datetime.now(timezone.utc).isoformat()

        print("=" * 60, flush=True)
        print("Refresco de estado en bloque (listado)", flush=True)
        print("=" * 60, flush=True)

        ids_existentes = cargar_ids_existentes(conn)
        print(f"contratos existentes en BD: {len(ids_existentes):,}", flush=True)

        t0 = time.time()
        total_vistos = 0
        por_estado: dict[str, int] = {}
        pendiente: list[dict] = []
        # El listado de SEACE repite el mismo idContrato entre páginas (orden
        # inestable en la API de búsqueda). Sin dedup, el UPSERT con on_conflict
        # revienta con «ON CONFLICT DO UPDATE command cannot affect row a second
        # time» (21000) apenas un lote trae dos filas con el mismo id.
        vistos: set[int] = set()

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            page = browser.new_context(ignore_https_errors=True).new_page()
            page.goto(SPA_URL, wait_until="networkidle", timeout=90_000)
            page.wait_for_timeout(2_000)

            total_api, lote, status = _api_call(page, 1)
            if status != 200:
                print(f"[error] página 1: HTTP {status}", flush=True)
                return
            total_pags = -(-total_api // PAGE_SIZE) if total_api else 0
            if args.limit_paginas:
                total_pags = min(total_pags, args.limit_paginas)
            print(f"totalElements={total_api:,}  páginas a leer={total_pags}",
                  flush=True)

            pagina = 1
            while pagina <= total_pags:
                if args.max_segundos and (time.time() - t0) >= args.max_segundos:
                    print(
                        f"[tope] {args.max_segundos}s alcanzados en p{pagina}/"
                        f"{total_pags}; el resto corre en la próxima pasada.",
                        flush=True,
                    )
                    break

                if pagina > 1:
                    _, lote, status = _api_call(page, pagina)
                    if status != 200:
                        print(f"[error] página {pagina}: HTTP {status} (continúa)",
                              flush=True)
                        pagina += 1
                        continue

                for r in lote:
                    cid = r.get("idContrato")
                    if cid is None:
                        continue
                    cid = int(cid)
                    if cid in vistos:
                        continue
                    vistos.add(cid)
                    if cid not in ids_existentes:
                        continue
                    estado = r.get("nomEstadoContrato")
                    por_estado[estado] = por_estado.get(estado, 0) + 1
                    total_vistos += 1
                    pendiente.append({
                        "id": cid,
                        "estado": estado,
                        "cotizar": bool(r.get("cotizar", False)),
                        "fecha_ini_cotizacion": parsear_fecha(r.get("fecIniCotizacion")),
                        "fecha_fin_cotizacion": parsear_fecha(r.get("fecFinCotizacion")),
                        "estado_verificado_at": now_iso,
                    })

                if len(pendiente) >= BATCH_DB:
                    n = upsert_lote(conn, pendiente)
                    print(f"  → upsert {n} filas (acum {total_vistos:,})", flush=True)
                    pendiente = []

                if pagina % 50 == 0 or pagina == total_pags:
                    elapsed = time.time() - t0
                    print(f"  p{pagina}/{total_pags} acum={total_vistos:,} "
                          f"{elapsed:.0f}s", flush=True)

                if not lote or pagina >= total_pags:
                    break
                pagina += 1
                time.sleep(DELAY_S)

            browser.close()

        if pendiente:
            upsert_lote(conn, pendiente)

        elapsed = time.time() - t0
        print(f"\n{'=' * 60}", flush=True)
        print(f"Refresco en bloque completado en {elapsed:.0f}s", flush=True)
        print(f"  Filas refrescadas: {total_vistos:,}", flush=True)
        print(f"  Por estado: {por_estado}", flush=True)
        print("=" * 60, flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
