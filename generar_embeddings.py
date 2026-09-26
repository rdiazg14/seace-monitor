#!/usr/bin/env python3
"""
Embeddings para chunks_tdr.

  gemini → columna embedding_v2(1536) WHERE embedding_v2 IS NULL
           SOLO chunks de contratos Vigente. Idempotente.

Uso:
  python generar_embeddings.py [--limit N]
  python generar_embeddings.py --cobertura
  python generar_embeddings.py --auth-check
"""
from __future__ import annotations

from seace_monitor.config import cargar_env

import argparse
import os
from pathlib import Path

import httpx
from seace_monitor.supabase_client import crear_cliente

from seace_monitor.embeddings.gemini_provider import (
    GEMINI_BACKOFF,
    GEMINI_DIM,
    GEMINI_EMBED_MODEL,
    GEMINI_EMBED_URL,
    QuotaExceeded,
    consultar_auth_gemini,
    solicitar_embeddings_gemini,
)
from seace_monitor.embeddings.preparation import (
    EMBED_STATS,
    MAX_CHARS_GEMINI,
    modo_embed_fila,
    print_embed_stats,
    reset_embed_stats,
    texto_para_embed,
    vec_literal,
)
from seace_monitor.embeddings.repository import (
    PAGE,
    chunks_sin_embedding_v2,
    chunks_sin_v2_por_fuente,
    contar_embeddings_v2,
    cobertura_vigentes,
    guardar_embeddings_v2,
    paginar_ids_vigentes,
    reset_embedding_v2,
)
from seace_monitor.embeddings.service import (
    BATCH_GEMINI,
    DELAY_GEMINI_S,
    print_cobertura as _print_cobertura,
    run_gemini as _run_gemini,
)
from seace_monitor.gemini import EMBED_USD_PER_M
cargar_env()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# Precio gemini-embedding-001: EMBED_USD_PER_M viene de seace_monitor.gemini.

def embed_lote_gemini(
    client: httpx.Client,
    texts: list[str],
    fail_fast: bool = False,
) -> list[list[float]]:
    """Conserva la API histórica delegando el transporte al adaptador."""
    return solicitar_embeddings_gemini(
        client,
        texts,
        GEMINI_API_KEY,
        fail_fast=fail_fast,
    )


def print_cobertura(cov: dict[str, int]) -> None:
    _print_cobertura(cov)


def run_gemini(
    supa,
    limit: int,
    fuente: str | None = None,
    ids: list[int] | None = None,
    batch: int | None = None,
    embed_mode: str = "auto",
    delay: float | None = None,
    fail_fast: bool = False,
) -> dict:
    """Fachada historica del CLI; la orquestacion vive en el paquete."""
    return _run_gemini(
        supa,
        limit,
        fuente=fuente,
        ids=ids,
        batch=batch,
        embed_mode=embed_mode,
        delay=delay,
        fail_fast=fail_fast,
        api_key=GEMINI_API_KEY,
    )

def auth_check_gemini() -> int:
    """Llama a Gemini y reporta solo el HTTP. No imprime la key ni el body."""
    if not GEMINI_API_KEY:
        print("gemini_auth HTTP=missing auth_ok=false", flush=True)
        return 2
    code = consultar_auth_gemini(httpx, GEMINI_API_KEY)
    auth_fail = code in (401, 403)
    ok = (not auth_fail) and code < 400
    print(f"gemini_auth HTTP={code} auth_fail={auth_fail} auth_ok={ok}", flush=True)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = todos")
    ap.add_argument(
        "--cobertura",
        action="store_true",
        help="Solo reporta cobertura v2 de vigentes; no embebe",
    )
    ap.add_argument(
        "--fuente",
        default="",
        help="Filtra chunks por fuente (api|pdf). Vacio = todas.",
    )
    ap.add_argument(
        "--ids",
        default="",
        help="Ids de contrato separados por coma (no re-embebe el resto)",
    )
    ap.add_argument(
        "--batch",
        type=int,
        default=0,
        help="Tamano de lote Gemini (0 = default 16)",
    )
    ap.add_argument(
        "--embed-mode",
        choices=["auto", "header", "body"],
        default="auto",
        help="auto: pdf=cuerpo sin header, api=texto completo. "
             "header/body fuerzan el modo (A/B del header PDF).",
    )
    ap.add_argument(
        "--delay",
        type=float,
        default=-1,
        help="Pausa entre lotes Gemini en segundos (-1 = auto: 8s si batch<=2)",
    )
    ap.add_argument(
        "--reset-v2",
        action="store_true",
        help="Pone embedding_v2=NULL en --ids + --fuente (no embebe). Exige ambos.",
    )
    ap.add_argument(
        "--fail-fast",
        action="store_true",
        help="Ante 429 PARA sin backoff (validacion de muestra).",
    )
    ap.add_argument(
        "--auth-check",
        action="store_true",
        help="Ping Gemini (embedContent) y sale. No toca la BD ni imprime la key.",
    )
    args = ap.parse_args()

    if args.auth_check:
        raise SystemExit(auth_check_gemini())

    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SystemExit("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY no encontrados")

    supa = crear_cliente()
    if args.cobertura:
        print_cobertura(cobertura_vigentes(supa))
        return

    ids = [int(x) for x in args.ids.replace(" ", "").split(",") if x] if args.ids else None
    if args.reset_v2:
        n = reset_embedding_v2(supa, ids or [], args.fuente)
        print(f"reset embedding_v2: {n} filas (fuente={args.fuente} ids={ids})", flush=True)
        return

    run_gemini(
        supa,
        args.limit,
        fuente=(args.fuente or None),
        ids=ids,
        batch=(args.batch or None),
        embed_mode=args.embed_mode,
        delay=(None if args.delay < 0 else args.delay),
        fail_fast=args.fail_fast,
    )


if __name__ == "__main__":
    main()
