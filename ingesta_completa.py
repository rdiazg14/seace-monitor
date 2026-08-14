#!/usr/bin/env python3
"""
Ingesta completa del corpus de contrataciones menores del SEACE.

- Primera corrida : descarga los ~76k registros completos.
- Corridas siguientes: incremental — lee el parquet existente, detecta el
  idContrato mas alto ya almacenado y solo baja lo publicado despues de ese.

Salida:
  data/seace_menores_completo.parquet  (snappy, todo el corpus)
  data/seace_menores_completo.csv      (utf-8-sig; si >100 MB solo ultimas 1000)
  data/ultima_ingesta.txt              (timestamp + total registros)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd
from playwright.sync_api import sync_playwright

URL_SPA = "https://prod6.seace.gob.pe/buscador-publico/contrataciones"
API = (
    "https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico"
    "/contrataciones/buscador"
)
ANIO = datetime.now().year
PAGE_SIZE = 100
CSV_LIMIT_BYTES = 100 * 1024 * 1024   # 100 MB (limite push GitHub sin LFS)
CSV_MUESTRA_ROWS = 1000
OUT_PARQUET = "data/seace_menores_completo.parquet"
OUT_CSV = "data/seace_menores_completo.csv"
OUT_LOG = "data/ultima_ingesta.txt"


def api_call(page, page_num: int) -> tuple[int, list[dict], int]:
    """Devuelve (totalElements, data_rows, status_code)."""
    r = page.request.get(
        API,
        params={
            "anio": ANIO,
            "palabra_clave": "",
            "orden": 2,
            "page": page_num,
            "page_size": PAGE_SIZE,
        },
        timeout=60_000,
    )
    if r.status != 200:
        return 0, [], r.status
    j = r.json()
    total = j.get("pageable", {}).get("totalElements", 0)
    data = j.get("data", []) or []
    return total, data, 200


def cargar_existente(path: str) -> pd.DataFrame:
    if os.path.exists(path):
        try:
            df = pd.read_parquet(path, engine="pyarrow")
            print(f"[incremental] parquet existente: {len(df):,} registros, "
                  f"max idContrato={df['idContrato'].max()}")
            return df
        except Exception as e:
            print(f"[aviso] no se pudo leer el parquet existente ({e}); "
                  "se hara ingesta completa.")
    return pd.DataFrame()


def guardar_csv(df: pd.DataFrame):
    os.makedirs("data", exist_ok=True)
    size_est = df.memory_usage(deep=True).sum()
    if size_est > CSV_LIMIT_BYTES:
        sub = df.sort_values("idContrato", ascending=False).head(CSV_MUESTRA_ROWS)
        sub.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
        real = os.path.getsize(OUT_CSV)
        print(f"[csv] corpus mayor a 100 MB estimado; se guardaron las ultimas "
              f"{CSV_MUESTRA_ROWS} filas -> {real/1024:.0f} KiB")
    else:
        df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
        real = os.path.getsize(OUT_CSV)
        print(f"[csv] {OUT_CSV} -> {real/1024/1024:.1f} MiB ({len(df):,} filas)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--forzar-completa", action="store_true",
                    help="ignora el parquet existente y re-descarga todo")
    ap.add_argument("--headed", action="store_true",
                    help="navegar con navegador visible (debug)")
    args = ap.parse_args()

    os.makedirs("data", exist_ok=True)

    df_existente = pd.DataFrame() if args.forzar_completa else cargar_existente(OUT_PARQUET)
    max_id_known = int(df_existente["idContrato"].max()) if not df_existente.empty else 0
    modo_incremental = max_id_known > 0

    if modo_incremental:
        print(f"[modo] INCREMENTAL — solo registros con idContrato > {max_id_known}")
    else:
        print("[modo] COMPLETA — descargando todo el corpus")

    t0 = time.time()
    nuevas: list[dict] = []
    stop = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(ignore_https_errors=True)
        page = ctx.new_page()

        print("Iniciando SPA para establecer sesion...")
        page.goto(URL_SPA, wait_until="networkidle", timeout=90_000)
        page.wait_for_timeout(2000)

        total_api = 0
        pagina = 1

        while not stop:
            total_api, lote, status = api_call(page, pagina)
            if status != 200:
                print(f"[error] pagina {pagina}: status {status}; abortando.")
                break
            if pagina == 1:
                total_paginas = -(-total_api // PAGE_SIZE)
                print(f"totalElements={total_api:,}  paginas_necesarias={total_paginas}")

            # Filtro incremental: descartar IDs ya conocidos
            if modo_incremental:
                filtrado = [r for r in lote if r.get("idContrato", 0) > max_id_known]
                if len(filtrado) < len(lote):
                    nuevas.extend(filtrado)
                    stop = True   # llegamos a registros ya almacenados
                    print(f"  pagina {pagina}: {len(filtrado)} nuevos "
                          f"(+ {len(lote)-len(filtrado)} ya conocidos) -> STOP")
                    break
                nuevas.extend(filtrado)
            else:
                nuevas.extend(lote)

            elapsed = time.time() - t0
            rate = pagina / elapsed if elapsed > 0 else 0
            remaining = (total_paginas - pagina) / rate if rate > 0 else 0
            print(f"  pagina {pagina}/{total_paginas} | +{len(lote)} filas "
                  f"| acum={len(nuevas):,} | {elapsed:.0f}s | ~{remaining:.0f}s restantes")

            if not lote or pagina >= total_paginas:
                break
            pagina += 1

        browser.close()

    print(f"\nDescargados: {len(nuevas):,} registros nuevos en {time.time()-t0:.0f}s")

    if not nuevas and not modo_incremental:
        print("ERROR: sin registros.", file=sys.stderr)
        sys.exit(1)

    df_nuevas = pd.DataFrame(nuevas)

    # Merge incremental
    if modo_incremental and not df_nuevas.empty:
        df_final = pd.concat([df_existente, df_nuevas], ignore_index=True)
        df_final = df_final.drop_duplicates("idContrato")
        print(f"Total corpus tras merge: {len(df_final):,} registros")
    elif modo_incremental and df_nuevas.empty:
        print("Sin registros nuevos; el corpus esta al dia.")
        df_final = df_existente
    else:
        df_final = df_nuevas

    # Guardar parquet
    df_final.to_parquet(OUT_PARQUET, engine="pyarrow", compression="snappy", index=False)
    pq_size = os.path.getsize(OUT_PARQUET)
    print(f"[parquet] {OUT_PARQUET} -> {pq_size/1024/1024:.2f} MiB ({len(df_final):,} filas)")

    # Guardar CSV (con limite GitHub)
    guardar_csv(df_final)

    # Log
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(OUT_LOG, "w", encoding="utf-8") as f:
        f.write(f"{ts}\ntotal_registros={len(df_final):,}\nnuevos_esta_corrida={len(nuevas):,}\n")
    print(f"[log] {OUT_LOG} -> {ts}")

    print("\n===== RESUMEN FINAL =====")
    print(f"Registros en corpus: {len(df_final):,}")
    print(f"Parquet:  {pq_size/1024/1024:.2f} MiB")
    csv_size = os.path.getsize(OUT_CSV)
    print(f"CSV:      {csv_size/1024/1024:.2f} MiB")
    if not df_final.empty:
        print(f"Columnas: {list(df_final.columns)}")
    print("=========================")


if __name__ == "__main__":
    main()
