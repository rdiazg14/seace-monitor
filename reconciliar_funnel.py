#!/usr/bin/env python3
"""
Copia marcas permanentes de funnel (#10/#11) desde el Worker a contratos.

GET /funnel-pendientes → upsert {analizado, cotizado, fecha_*} con la fecha
del KV (primera marca). Idempotente: reescribe el mismo ISO, no usa now().

Uso:
  python reconciliar_funnel.py
  python reconciliar_funnel.py --dry-run
  python reconciliar_funnel.py --dry-run --limit 10
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
FUNNEL_TOKEN = os.getenv("FUNNEL_TOKEN", "")
FUNNEL_WORKER_URL = os.getenv(
    "FUNNEL_WORKER_URL",
    "https://seace-ai-proxy.rdiazg14.workers.dev",
)
BATCH_DB = 100


def init_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[supabase] variables de entorno no configuradas — solo CSV/parquet.")
        return None
    try:
        from supabase import create_client
        client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        print("[supabase] cliente inicializado OK")
        return client
    except ImportError:
        print("[aviso] paquete 'supabase' no instalado.")
        return None
    except Exception as e:
        print(f"[aviso] error al conectar Supabase: {e}")
        return None


def _int_id(raw) -> int | None:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if isinstance(raw, float) and raw != n:
        return None
    return n


def _fecha_iso(raw) -> str | None:
    """Devuelve el string original si es ISO parseable. No reformatea ni usa now()."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return s


def _item(raw) -> tuple[int, str] | None:
    if not isinstance(raw, dict):
        return None
    cid = _int_id(raw.get("id"))
    fecha = _fecha_iso(raw.get("fecha"))
    if cid is None or fecha is None:
        return None
    return cid, fecha


def fusionar(payload: dict) -> tuple[list[dict], int, int, int]:
    """Un dict por id. Un contrato en ambas listas lleva las 4 claves."""
    if not isinstance(payload, dict):
        raise ValueError("respuesta del Worker no es un objeto JSON")
    by_id: dict[int, dict] = {}
    n_a = 0
    n_c = 0
    n_desc = 0
    n_fusion = 0

    for raw in payload.get("analizados") or []:
        parsed = _item(raw)
        if parsed is None:
            n_desc += 1
            continue
        cid, fecha = parsed
        by_id[cid] = {
            "id": cid,
            "analizado": True,
            "fecha_analisis": fecha,
        }
        n_a += 1

    for raw in payload.get("cotizados") or []:
        parsed = _item(raw)
        if parsed is None:
            n_desc += 1
            continue
        cid, fecha = parsed
        if cid in by_id:
            n_fusion += 1
            print(
                f"  fusion id={cid} analizado+cotizado "
                f"(fecha_analisis={by_id[cid].get('fecha_analisis')} "
                f"fecha_cotizacion={fecha})",
                flush=True,
            )
        row = by_id.setdefault(cid, {"id": cid})
        row["cotizado"] = True
        row["fecha_cotizacion"] = fecha
        n_c += 1

    filas = [by_id[k] for k in sorted(by_id)]
    if n_desc:
        print(f"  descartados (id/fecha inválidos): {n_desc}", flush=True)
    if n_fusion:
        print(f"  fusionados en un upsert: {n_fusion}", flush=True)
    return filas, n_a, n_c, n_fusion


def fetch_funnel(http: httpx.Client, url: str, token: str) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    waits = [2.0, 4.0, 8.0]
    last_err: Exception | None = None
    for attempt, wait in enumerate([0.0] + waits):
        if wait:
            time.sleep(wait)
        try:
            r = http.get(url, headers=headers, timeout=60.0)
            if r.status_code == 401:
                print(
                    "ERROR: Worker 401 unauthorized — "
                    "FUNNEL_TOKEN no coincide entre Worker y este entorno "
                    "(Actions / .env).",
                    flush=True,
                )
                raise SystemExit(1)
            r.raise_for_status()
            body = r.json()
            if not isinstance(body, dict):
                raise RuntimeError("respuesta inesperada: no es un objeto")
            return body
        except SystemExit:
            raise
        except httpx.HTTPStatusError as e:
            last_err = e
            code = e.response.status_code if e.response is not None else 0
            if code == 401:
                print(
                    "ERROR: Worker 401 unauthorized — "
                    "FUNNEL_TOKEN no coincide entre Worker y este entorno "
                    "(Actions / .env).",
                    flush=True,
                )
                raise SystemExit(1)
            print(f"    [retry {attempt}] HTTP {code}: {e}", flush=True)
        except Exception as e:
            last_err = e
            print(f"    [retry {attempt}] {e}", flush=True)
    print(f"ERROR: GET /funnel-pendientes falló: {last_err}", flush=True)
    raise SystemExit(1)


def upsert_lote(supa, lote: list[dict]) -> None:
    supa.table("contratos").upsert(lote, on_conflict="id").execute()


def flush_upsert(supa, pendiente: list[dict], dry_run: bool) -> None:
    if not pendiente or dry_run:
        pendiente.clear()
        return
    try:
        upsert_lote(supa, pendiente)
        print(f"    → upsert lote {len(pendiente)} filas OK", flush=True)
    except Exception as e:
        print(f"    → upsert lote FALLÓ: {e}", flush=True)
    pendiente.clear()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="GET al Worker y loguea; no escribe en BD")
    ap.add_argument("--limit", type=int, default=0,
                    help="Tope de contratos a upsert (0 = todos)")
    args = ap.parse_args()

    token = (FUNNEL_TOKEN or "").strip()
    if not token:
        print(
            "ERROR: FUNNEL_TOKEN no configurado "
            "(secret de Actions o .env). No llamo al Worker.",
            flush=True,
        )
        raise SystemExit(1)

    base = (FUNNEL_WORKER_URL or "").rstrip("/")
    url = f"{base}/funnel-pendientes"

    print("=" * 60, flush=True)
    print("Funnel — reconciliar marcas KV → contratos", flush=True)
    print(f"  dry-run={args.dry_run}  limit={args.limit or 'all'}", flush=True)
    print(f"  endpoint={url}", flush=True)
    print("=" * 60, flush=True)

    with httpx.Client() as http:
        payload = fetch_funnel(http, url, token)

    filas, n_a, n_c, n_fusion = fusionar(payload)
    if args.limit and args.limit > 0:
        filas = filas[: args.limit]
        print(f"  --limit {args.limit}: {len(filas)} filas a procesar", flush=True)

    print(f"  ids Worker: analizados={n_a} cotizados={n_c} "
          f"unicos={n_a + n_c - n_fusion} fusion={n_fusion}", flush=True)

    if args.dry_run:
        print("  [dry-run] UPDATE que se haría (sin escribir):", flush=True)
        for row in filas:
            print(f"    {row}", flush=True)
        print(
            f"reconciliados: {n_a} analizados, {n_c} cotizados (dry-run)",
            flush=True,
        )
        return

    supa = init_supabase()
    if supa is None:
        print("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY no encontrados", flush=True)
        raise SystemExit(1)

    pendiente: list[dict] = []
    for row in filas:
        pendiente.append(row)
        if len(pendiente) >= BATCH_DB:
            flush_upsert(supa, pendiente, False)
    flush_upsert(supa, pendiente, False)

    print(
        f"reconciliados: {n_a} analizados, {n_c} cotizados (escrito)",
        flush=True,
    )


if __name__ == "__main__":
    main()
