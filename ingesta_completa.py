#!/usr/bin/env python3
"""
Ingesta completa del corpus de contrataciones menores del SEACE.

Modos:
  COMPLETA     Primera corrida o --forzar-completa. Descarga los ~76k registros.
  INCREMENTAL  Corridas siguientes. Detecta MAX(id) en Supabase (o parquet local)
               y solo baja lo publicado después de ese punto.

Salidas:
  Supabase  tabla 'contratos' via UPSERT — si SUPABASE_URL y
            SUPABASE_SERVICE_KEY están en el entorno.
  data/seace_menores_completo.parquet   backup local (snappy)
  data/seace_menores_completo.csv       utf-8-sig; si >100 MB solo últimas 1000 filas
  data/ultima_ingesta.txt               timestamp + estadísticas
"""
from __future__ import annotations

from seace_monitor.config import cargar_env
from seace_monitor.classification.keywords import cargar_keywords
from seace_monitor.ingestion.models import (
    RegistroSeace,
    filtrar_validos as validar_registros_seace,
    id_contrato_de as _id_contrato_de,
    payload_solo_datos,
)
from seace_monitor.ingestion.repository import (
    COLS_CONTRATOS as _COLS_CONTRATOS,
    get_max_id as obtener_max_id,
    registrar_rechazo,
    upsert_contratos_pg,
    upsert_lote as persistir_lote,
    upsert_supabase as persistir_supabase,
)
from seace_monitor.ingestion.service import preparar_fila_db as transformar_fila_db

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from playwright.sync_api import sync_playwright
from pydantic import ValidationError

from seace_monitor.logging import PASO_INGESTA, registrar_run
from seace_monitor.clasificacion import (
    IT_CATS,
    KW_ALTA,
    KW_GENERICOS,
    _norm,
    _contiene,
    _texto_contrato,
    _match_kw_tabla,
    clasificar_categoria_it,
    clasificar_relevancia_ia,
)
from seace_monitor.seace_api import API_BUSCADOR, SPA_URL, parsear_fecha

cargar_env()

# ── Configuración ──────────────────────────────────────────────────────────
ANIO        = datetime.now().year
PAGE_SIZE   = 100
BATCH_SIZE  = 500                   # registros por lote de upsert a Supabase
CSV_LIMITE  = 100 * 1024 * 1024    # 100 MiB
CSV_MUESTRA = 1_000

OUT_PARQUET = "data/seace_menores_completo.parquet"
OUT_CSV     = "data/seace_menores_completo.csv"
OUT_LOG     = "data/ultima_ingesta.txt"

# Credenciales (GitHub Secrets en Actions; .env local en desarrollo)
SUPABASE_URL         = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")


# Nunca persistir contexto de sesión Playwright / HTTP.
_KEYS_SESION = {
    "cookie", "cookies", "authorization", "token", "access_token",
    "refresh_token", "set-cookie", "headers", "header", "csrf",
    "x-csrf-token", "api_key", "apikey", "session", "playwright",
    "request", "response",
}


def filtrar_validos(raw: list[dict], client) -> tuple[list[dict], int]:
    return validar_registros_seace(
        raw,
        lambda payload, motivo: registrar_rechazo(client, payload, motivo),
    )


def preparar_fila_db(
    r: dict,
    cats: list[tuple[str, list[dict]]] | None = None,
) -> dict:
    del cats
    return transformar_fila_db(r)


def escribir_clasificacion_keyword(
    filas_cls: list[dict],
    *,
    supa=None,
) -> tuple[int, int]:
    """Tras el upsert de contratos: capa=keyword. Eco copia a contratos."""
    if not filas_cls:
        return 0, 0
    from seace_monitor.classification.repository import escribir_keyword

    return escribir_keyword(filas_cls, artefacto="ingesta", supa=supa)


# ── Supabase ────────────────────────────────────────────────────────────────

def init_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[supabase] variables de entorno no configuradas — solo CSV/parquet.")
        return None
    try:
        from seace_monitor.supabase_client import crear_cliente
        client = crear_cliente()
        print("[supabase] cliente inicializado OK")
        return client
    except ImportError:
        print("[aviso] paquete 'supabase' no instalado.")
        return None
    except Exception as e:
        print(f"[aviso] error al conectar Supabase: {e}")
        return None


def get_max_id_supabase(client) -> int:
    return obtener_max_id(client)


def _upsert_lote(client, lote: list[dict], reintentos: int = 3):
    return persistir_lote(client, lote, reintentos)


def upsert_supabase(client, filas: list[dict]) -> int:
    return persistir_supabase(client, filas, batch_size=BATCH_SIZE)


# ── Descarga desde API SEACE ─────────────────────────────────────────────────

def _api_call(page, page_num: int) -> tuple[int, list[dict], int]:
    r = page.request.get(
        API_BUSCADOR,
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


# ── Parquet / CSV ────────────────────────────────────────────────────────────

def _leer_parquet() -> pd.DataFrame:
    if os.path.exists(OUT_PARQUET):
        try:
            df = pd.read_parquet(OUT_PARQUET, engine="pyarrow")
            print(f"[parquet] existente: {len(df):,} filas, "
                  f"max idContrato={df['idContrato'].max()}")
            return df
        except Exception as e:
            print(f"[aviso] parquet no legible ({e})")
    return pd.DataFrame()


def _guardar_csv(df: pd.DataFrame):
    est = df.memory_usage(deep=True).sum()
    if est > CSV_LIMITE:
        sub = df.sort_values("idContrato", ascending=False).head(CSV_MUESTRA)
        sub.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
        sz = os.path.getsize(OUT_CSV) / 1024
        print(f"[csv] corpus > 100 MiB → muestra de {CSV_MUESTRA} filas "
              f"({sz:.0f} KiB)")
    else:
        df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
        sz = os.path.getsize(OUT_CSV) / 1024 / 1024
        print(f"[csv] {OUT_CSV} → {sz:.1f} MiB ({len(df):,} filas)")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Ingesta corpus SEACE → Supabase + parquet + CSV"
    )
    ap.add_argument("--forzar-completa", action="store_true",
                    help=(
                        "ignora max_id y re-descarga todo el corpus. "
                        "Ya no pisa categoria_it: la inferencia vive en "
                        "clasificacion_contrato (capa 3) y keywords no pisan "
                        "gemini/humano."
                    ))
    ap.add_argument("--headed", action="store_true",
                    help="navegador visible (útil para depurar)")
    ap.add_argument("--simular-rechazo", action="store_true",
                    help="G2: inserta un payload inválido en ingesta_rechazados y sale")
    ap.add_argument(
        "--verificar-keywords",
        action="store_true",
        help=(
            "Compara categoria tabla vs IT_CATS en cada alta y reporta DIFF. "
            "No cambia lo escrito (siempre se persiste el resultado de la tabla). "
            "La equivalencia se probo sobre el corpus historico; este flag la "
            "verifica sobre datos frescos de SEACE."
        ),
    )
    args = ap.parse_args()
    os.makedirs("data", exist_ok=True)

    # ── 1. Iniciar Supabase ────────────────────────────────────────────
    supa = init_supabase()

    if args.simular_rechazo:
        fake = {
            "desObjetoContrato": "SIMULACION G2 — sin idContrato",
            "nomEntidad": "ENTIDAD DE PRUEBA",
            "nomEstadoContrato": {"inesperado": True},
        }
        print("G2 --simular-rechazo: payload inválido a propósito", flush=True)
        print(json.dumps(fake, ensure_ascii=False), flush=True)
        try:
            RegistroSeace.model_validate(fake)
            raise SystemExit("ERROR: el payload simulado no debería pasar el esquema")
        except ValidationError as e:
            motivo = str(e)
            print(f"  ValidationError:\n{motivo}", flush=True)
            registrar_rechazo(supa, fake, motivo, origen="ingesta")
        if not supa:
            raise SystemExit("ERROR: sin Supabase; el rechazo no se persistió")
        try:
            res = (
                supa.table("ingesta_rechazados")
                .select("id,id_contrato,origen,motivo,payload,resuelto,created_at")
                .order("id", desc=True)
                .limit(1)
                .execute()
            )
        except Exception as e:
            raise SystemExit(
                f"ERROR: no pude leer ingesta_rechazados ({e}). "
                "Pega ingesta_rechazados.sql en Supabase → SQL Editor y reintenta."
            )
        row = (res.data or [None])[0]
        if not row:
            raise SystemExit(
                "ERROR: no hay fila en ingesta_rechazados. "
                "Pega ingesta_rechazados.sql en Supabase → SQL Editor y reintenta."
            )
        print("\nFila persistida:", flush=True)
        print(json.dumps(row, ensure_ascii=False, indent=2, default=str), flush=True)
        return

    # ── 1b. Keywords IT (tabla; fallback explicito a IT_CATS) ──────────
    cats_tabla = cargar_keywords(supa)
    if cats_tabla is None:
        if not supa:
            motivo = "sin cliente Supabase"
        else:
            motivo = "tabla vacia o SELECT fallo"
        print(f"[keywords] FALLBACK a IT_CATS hardcoded: {motivo}", flush=True)
        cats: list[tuple[str, list[dict]]] | None = None
    elif len(cats_tabla) < 13:
        print(
            f"[keywords] FALLBACK a IT_CATS hardcoded: "
            f"{len(cats_tabla)} categorias (hace falta 13)",
            flush=True,
        )
        cats = None
    else:
        n_kw = sum(len(kws) for _, kws in cats_tabla)
        print(
            f"[keywords] tabla it_keywords: {n_kw} keywords, "
            f"{len(cats_tabla)} categorias",
            flush=True,
        )
        cats = cats_tabla

    # ── 2. Determinar punto de partida (incremental vs completo) ───────
    max_id = 0
    if not args.forzar_completa:
        if supa:
            max_id = get_max_id_supabase(supa)
            print(f"[incremental] MAX id en Supabase: {max_id:,}")
        else:
            df_local = _leer_parquet()
            if not df_local.empty:
                max_id = int(df_local["idContrato"].max())
                print(f"[incremental] MAX id en parquet local: {max_id:,}")

    modo = "INCREMENTAL" if max_id > 0 else "COMPLETA"
    print(f"[modo] {modo}" + (f" — solo id > {max_id:,}" if max_id else ""))

    # ── 3. Descarga desde el SEACE ────────────────────────────────────
    t0 = time.time()
    nuevas_raw: list[dict] = []
    alerta_anomala = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_context(ignore_https_errors=True).new_page()
        print("Iniciando SPA...")
        page.goto(SPA_URL, wait_until="networkidle", timeout=90_000)
        page.wait_for_timeout(2_000)

        total_api  = 0
        total_pags = 1
        pagina     = 1
        stop       = False

        while not stop:
            total_api, lote, status = _api_call(page, pagina)
            if status != 200:
                print(f"[error] pagina {pagina}: HTTP {status}")
                alerta_anomala = 1
                break
            if pagina == 1:
                total_pags = -(-total_api // PAGE_SIZE) if total_api else 0
                print(f"totalElements={total_api:,}  paginas={total_pags}")
                if total_api == 0:
                    alerta_anomala = 1

            if max_id > 0:
                nuevos = [r for r in lote if r.get("idContrato", 0) > max_id]
                if len(nuevos) < len(lote):
                    nuevas_raw.extend(nuevos)
                    print(f"  p{pagina}: {len(nuevos)} nuevos "
                          f"({len(lote)-len(nuevos)} ya conocidos) -> STOP")
                    stop = True
                    break
                nuevas_raw.extend(nuevos)
            else:
                nuevas_raw.extend(lote)

            elapsed = time.time() - t0
            rate = pagina / max(elapsed, 1)
            eta  = (total_pags - pagina) / rate
            print(f"  p{pagina}/{total_pags} +{len(lote)} "
                  f"acum={len(nuevas_raw):,} {elapsed:.0f}s ~{eta:.0f}s")

            if not lote or pagina >= total_pags:
                break
            pagina += 1

        browser.close()

    print(f"\nDescarga: {len(nuevas_raw):,} registros en {time.time()-t0:.0f}s")

    if not nuevas_raw and max_id == 0:
        alerta_anomala = 1
        print("ERROR: sin registros.", file=sys.stderr)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with open(OUT_LOG, "w", encoding="utf-8") as fh:
            fh.write(
                f"{ts}\n"
                f"total_registros=0\n"
                f"nuevos_esta_corrida=0\n"
                f"rechazados_esta_corrida=0\n"
                f"alerta_anomala=1\n"
            )
        registrar_run(
            supa,
            PASO_INGESTA,
            {
                "total_registros": 0,
                "nuevos_esta_corrida": 0,
                "rechazados_esta_corrida": 0,
                "alerta_anomala": 1,
            },
        )
        sys.exit(1)

    # ── 4. Validar (G2) + clasificar ───────────────────────────────────
    n_rech = 0
    filas_cls: list[dict] = []
    if nuevas_raw:
        print("Validando esquema (G2)...", flush=True)
        nuevas_raw, n_rech = filtrar_validos(nuevas_raw, supa)
        print(f"  aceptados={len(nuevas_raw):,}  rechazados={n_rech:,}", flush=True)
        print("Clasificando registros (capa 3, no en upsert contratos)...")
        filas_db: list[dict] = []
        n_kw_diff = 0
        for r in nuevas_raw:
            try:
                fila = preparar_fila_db(r, cats)
            except Exception as e:
                n_rech += 1
                registrar_rechazo(supa, r, f"preparar_fila_db: {e}")
                continue
            cat = clasificar_categoria_it(r, cats)
            ia = clasificar_relevancia_ia(r)
            if args.verificar_keywords:
                codigo = clasificar_categoria_it(r)
                if cat != codigo:
                    print(
                        f"[keywords DIFF] id={fila['id']} "
                        f"tabla={cat} codigo={codigo}",
                        flush=True,
                    )
                    n_kw_diff += 1
            filas_db.append(fila)
            if cat or ia:
                filas_cls.append({
                    "contrato_id": int(fila["id"]),
                    "categoria_it": cat,
                    "relevancia_ia": ia,
                })
        n_it = sum(1 for f in filas_cls if f.get("categoria_it"))
        n_ia = sum(1 for f in filas_cls if f.get("relevancia_ia"))
        print(f"  categoria_it asignada: {n_it:,}  |  relevancia_ia: {n_ia:,}")
        if args.verificar_keywords:
            print(f"  [keywords] diffs tabla vs IT_CATS: {n_kw_diff}", flush=True)
    else:
        filas_db = []
        print("Sin registros nuevos. Corpus al día.")
        if args.verificar_keywords:
            print("[keywords] diffs tabla vs codigo: 0 / 0", flush=True)

    # ── 5. Upsert a Supabase (hechos SEACE, sin inferencia) ────────────
    if filas_db:
        dsn = (os.getenv("DATABASE_URL") or "").strip()
        if dsn:
            try:
                upsert_contratos_pg(dsn, filas_db)
                print(f"[upsert] backend=psycopg ({len(filas_db)} filas)", flush=True)
            except Exception as e:
                print(f"[upsert] psycopg falló ({e}); fallback a supabase-py", flush=True)
                if supa:
                    upsert_supabase(supa, filas_db)
        elif supa:
            upsert_supabase(supa, filas_db)

        if filas_cls:
            from seace_monitor.classification.repository import anunciar_backend_capa3
            anunciar_backend_capa3(supa=supa)
            n_w, n_s = escribir_clasificacion_keyword(filas_cls, supa=supa)
            print(
                f"[clasificacion] keyword escritos={n_w} saltados_protegidos={n_s}",
                flush=True,
            )

    # ── 6. Guardar parquet + CSV (backup local) ────────────────────────
    df_nuevo = pd.DataFrame(nuevas_raw) if nuevas_raw else pd.DataFrame()
    df_exist = _leer_parquet()

    if not df_nuevo.empty:
        if not df_exist.empty:
            df_final = pd.concat([df_exist, df_nuevo], ignore_index=True)
            df_final = df_final.drop_duplicates("idContrato")
            print(f"[merge] total parquet tras merge: {len(df_final):,} filas")
        else:
            df_final = df_nuevo
    else:
        df_final = df_exist

    if not df_final.empty:
        df_final.to_parquet(OUT_PARQUET, engine="pyarrow",
                            compression="snappy", index=False)
        pq_sz = os.path.getsize(OUT_PARQUET)
        print(f"[parquet] {OUT_PARQUET} → {pq_sz/1024/1024:.2f} MiB "
              f"({len(df_final):,} filas)")
        _guardar_csv(df_final)

    # ── 7. Log ────────────────────────────────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    n_total = len(df_final) if not df_final.empty else 0
    with open(OUT_LOG, "w", encoding="utf-8") as fh:
        fh.write(
            f"{ts}\n"
            f"total_registros={n_total:,}\n"
            f"nuevos_esta_corrida={len(nuevas_raw):,}\n"
            f"rechazados_esta_corrida={n_rech}\n"
            f"alerta_anomala={alerta_anomala}\n"
        )
    registrar_run(
        supa,
        PASO_INGESTA,
        {
            "total_registros": n_total,
            "nuevos_esta_corrida": len(nuevas_raw),
            "rechazados_esta_corrida": n_rech,
            "alerta_anomala": alerta_anomala,
        },
    )
    print(f"[log] {ts}")

    print("\n===== RESUMEN FINAL =====")
    print(f"Total corpus local: {n_total:,}")
    print(f"Nuevos esta corrida: {len(nuevas_raw):,}")
    print(f"Rechazados (G2): {n_rech:,}")
    print(f"Supabase: {'OK upsert' if supa and filas_db else 'sin upsert (no config o sin altas)'}")
    print("=========================")


if __name__ == "__main__":
    main()
