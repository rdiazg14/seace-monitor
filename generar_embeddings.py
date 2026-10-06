#!/usr/bin/env python3
"""
Embeddings para chunks_tdr (dos espacios vectoriales, IA-007).

  gemini → columna embedding_v2(1536) WHERE embedding_v2 IS NULL
           SOLO chunks de contratos Vigente. Idempotente.
  qwen   → columna embedding_v3(1536) WHERE embedding_v3 IS NULL
           text-embedding-v4 (espacio qwen-tev4-1536). Idempotente.

Uso:
  python generar_embeddings.py [--espacio gemini|qwen] [--limit N]
  python generar_embeddings.py --cobertura
  python generar_embeddings.py --auth-check [--espacio qwen]
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
from seace_monitor.embeddings.openai_provider import solicitar_embeddings_openai
from seace_monitor.embeddings.repository import (
    PAGE,
    chunks_sin_embedding_v2,
    chunks_sin_v2_por_fuente,
    contar_embeddings_v2,
    cobertura_columna,
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
from seace_monitor.ia.errores import ErrorProveedor
from seace_monitor.ia.pipeline import cfg_resuelta, extras_embeddings
cargar_env()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# Precio gemini-embedding-001: EMBED_USD_PER_M viene de seace_monitor.gemini.

# Espacio qwen-tev4-1536 (embedding_v3): destino fijo, no se resuelve vía
# ia_endpoints.embeddings porque esa fila refleja siempre el corpus activo.
QWEN_EMBED_URL = "https://maas.qwencloudapi.com/compatible-mode/v1/embeddings"
QWEN_EMBED_MODEL = "text-embedding-v4"
QWEN_EMBED_USD_PER_M = 0.07  # espejo de ia_modelos precio (solo entrada)
BATCH_QWEN = 10              # text-embedding-v4: lotes <= 10 por petición

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
    espacio: str = "gemini",
) -> dict:
    """Fachada historica del CLI; la orquestacion vive en el paquete."""
    if espacio == "qwen":
        # El escritor v3 va a un modelo fijo: no pasa por
        # extras_embeddings/cfg_resuelta (esa fila describe el corpus activo).
        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not api_key:
            raise SystemExit(
                "ERROR: DASHSCOPE_API_KEY no encontrado (env / GitHub secret)"
            )
        solicitar = lambda http, texts, key, fail_fast=False: (  # noqa: E731
            solicitar_embeddings_openai(
                http, texts, key, fail_fast=fail_fast,
                url=QWEN_EMBED_URL, modelo=QWEN_EMBED_MODEL, dimensiones=1536,
                batch_max=BATCH_QWEN, proveedor="qwen",
            )
        )
        return _run_gemini(
            supa,
            limit,
            fuente=fuente,
            ids=ids,
            batch=(batch or BATCH_QWEN),
            embed_mode=embed_mode,
            delay=delay,
            fail_fast=fail_fast,
            api_key=api_key,
            solicitar=solicitar,
            modelo=QWEN_EMBED_MODEL,
            precio_in=QWEN_EMBED_USD_PER_M,
            columna="embedding_v3",
        )
    # El escritor v2 solo adopta la config cuando el endpoint 'embeddings'
    # apunta al espacio gemini; con corpus=qwen la config describe el espacio
    # activo y v2 sigue por env (FIX-012, sin mezcla de espacios).
    extras = extras_embeddings(
        cfg_resuelta("embeddings"), espacio="gemini-emb001-1536"
    )
    api_key = extras.pop("api_key", GEMINI_API_KEY)
    return _run_gemini(
        supa,
        limit,
        fuente=fuente,
        ids=ids,
        batch=batch,
        embed_mode=embed_mode,
        delay=delay,
        fail_fast=fail_fast,
        api_key=api_key,
        columna=extras.pop("columna", "embedding_v2"),
        **extras,
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


def auth_check_qwen() -> int:
    """Ping text-embedding-v4 y reporta solo el HTTP. No imprime la key ni el body."""
    api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key:
        print("qwen_auth HTTP=missing auth_ok=false", flush=True)
        return 2
    try:
        with httpx.Client() as client:
            solicitar_embeddings_openai(
                client,
                ["ping auth"],
                api_key,
                fail_fast=True,
                url=QWEN_EMBED_URL,
                modelo=QWEN_EMBED_MODEL,
                dimensiones=1536,
                batch_max=1,
                proveedor="qwen",
            )
        code = 200
    except ErrorProveedor as error:
        code = error.status or 0
    except QuotaExceeded:
        code = 429
    except Exception:
        code = 0
    auth_fail = code in (401, 403)
    ok = (not auth_fail) and 200 <= code < 400
    print(f"qwen_auth HTTP={code} auth_fail={auth_fail} auth_ok={ok}", flush=True)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = todos")
    ap.add_argument(
        "--espacio",
        choices=["gemini", "qwen"],
        default="gemini",
        help="Espacio destino: gemini=embedding_v2 (corpus activo), "
             "qwen=embedding_v3 (text-embedding-v4).",
    )
    ap.add_argument(
        "--cobertura",
        action="store_true",
        help="Solo reporta cobertura v2 y v3 de vigentes; no embebe",
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
        help="Tamano de lote (0 = default: 16 gemini / 10 qwen)",
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
        raise SystemExit(
            auth_check_qwen() if args.espacio == "qwen" else auth_check_gemini()
        )

    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SystemExit("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY no encontrados")

    supa = crear_cliente()
    if args.cobertura:
        print("Espacio gemini-emb001-1536:", flush=True)
        print_cobertura(cobertura_columna(supa, "embedding_v2"))
        print("Espacio qwen-tev4-1536:", flush=True)
        print_cobertura(cobertura_columna(supa, "embedding_v3"))
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
        espacio=args.espacio,
    )


if __name__ == "__main__":
    main()
