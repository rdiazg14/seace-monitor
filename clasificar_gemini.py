#!/usr/bin/env python3
"""
Fase B / C1 / C4: clasifica categoria_it con Gemini sobre lo que keywords
dejo sin fila en clasificacion_contrato.

Cascada: SELECT siempre sin fila en clasificacion_contrato.
No pisa etiquetas de keywords. No toca relevancia_ia. No escribe
flash_ocr_cuota.json (cupo propio: BD pipeline_cuota_c4; respaldo local en
data/clasificacion_cuota.json).

C4 semanal (clasificacion_semanal.yml): 3x --proponer + --consenso + --aplicar
sobre --filtro vigentes (ventana abierta o futura). No va al pipeline diario.

temperature:0 no es determinista: el consenso de 3 elimina varianza.
Nunca aplicar un consenso de menos de 3 corridas.

Este archivo compone configuración, wiring y comandos. El dominio vive en el
paquete: classification.cuota (tope C4 + tokens), classification.seleccion
(universo), classification.pasada (lotes P1/P2), classification.artefactos
(JSON), classification.ledger (rechazadas), classification.cola (revisión) y
classification.workflow (comandos). Se preservan exit codes, rutas de
artefactos y semántica de consenso/ledger/cola.

C1 (preferido):
    python clasificar_gemini.py --proponer --filtro vigentes
    python clasificar_gemini.py --consenso data/propuestas_it_A.json data/propuestas_it_B.json
    python clasificar_gemini.py --aplicar data/consenso_it_YYYYMMDD-HHMMSS.json
"""
from __future__ import annotations

from seace_monitor.classification.artefactos import DATA_DIR
from seace_monitor.classification.cuota import (
    CUOTA_C4_PATH,
    EXIT_CUPO_C4,
    MAX_LLAMADAS_DIA_DEFAULT,
)
from seace_monitor.classification.seleccion import paginar_nulls
from seace_monitor.classification.workflow import (
    ConfigClasificacion,
    camino_directo,
    comando_aplicar,
    comando_consenso,
    comando_proponer,
)
from seace_monitor.config import cargar_env

import argparse
import os
import sys
from functools import partial

from seace_monitor.classification.openai_provider import clasificar_lote_openai
from seace_monitor.ia.openai import url_openai
from seace_monitor.ia.pipeline import (
    cfg_resuelta,
    clave_config,
    url_gemini_generate,
)
from seace_monitor.supabase_client import crear_cliente

from vocabulario import cargar_pistas, registrar_desde_items

cargar_env()

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_FLASH = (
    os.getenv("GEMINI_FLASH_MODEL", "").strip() or "gemini-3.7-flash"
)
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_FLASH}:generateContent"
)


def resolver_cfg(endpoint: str):
    """Costura inyectable: config dinámica ia_* o None → camino por env."""
    return cfg_resuelta(endpoint)


def init_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY no encontrados",
              flush=True)
        return None
    try:
        client = crear_cliente()
        print("[supabase] cliente inicializado OK", flush=True)
        return client
    except Exception as e:
        print(f"ERROR: no se pudo conectar a Supabase: {e}", flush=True)
        return None


def supa_opcional():
    """Cliente silencioso para ledger/cola cuando el comando no lo creó."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    try:
        return crear_cliente()
    except Exception:
        return None


def construir_config(args, supa=None) -> ConfigClasificacion:
    cfg_ia = resolver_cfg("clasificar")
    if cfg_ia is not None:
        key = clave_config(cfg_ia)
        if key and cfg_ia.tipo_api == "openai":
            return ConfigClasificacion(
                api_key=key,
                url=url_openai(cfg_ia.base_url or "", "chat/completions"),
                modelo=cfg_ia.modelo,
                supa=supa,
                supa_opcional=supa_opcional,
                conectar_supa=init_supabase,
                max_llamadas=args.max_llamadas_dia,
                data_dir=DATA_DIR,
                cuota_path=CUOTA_C4_PATH,
                cargar_pistas=cargar_pistas,
                registrar_keywords=registrar_desde_items,
                transporte=partial(
                    clasificar_lote_openai,
                    modelo=cfg_ia.modelo,
                    params=cfg_ia.params,
                    proveedor=cfg_ia.proveedor,
                ),
                timeout=cfg_ia.timeout_ms / 1000.0,
                version_config=cfg_ia.version_config,
            )
        if key and cfg_ia.tipo_api == "gemini":
            return ConfigClasificacion(
                api_key=key,
                url=url_gemini_generate(cfg_ia),
                modelo=cfg_ia.modelo,
                supa=supa,
                supa_opcional=supa_opcional,
                conectar_supa=init_supabase,
                max_llamadas=args.max_llamadas_dia,
                data_dir=DATA_DIR,
                cuota_path=CUOTA_C4_PATH,
                cargar_pistas=cargar_pistas,
                registrar_keywords=registrar_desde_items,
                timeout=cfg_ia.timeout_ms / 1000.0,
                version_config=cfg_ia.version_config,
            )
        print(
            f"  [aviso] ia_endpoints.clasificar sin clave utilizable o tipo "
            f"{cfg_ia.tipo_api!r} no soportado; camino por env",
            flush=True,
        )
    return ConfigClasificacion(
        api_key=GEMINI_API_KEY,
        url=GEMINI_URL,
        modelo=GEMINI_FLASH,
        supa=supa,
        supa_opcional=supa_opcional,
        conectar_supa=init_supabase,
        max_llamadas=args.max_llamadas_dia,
        data_dir=DATA_DIR,
        cuota_path=CUOTA_C4_PATH,
        cargar_pistas=cargar_pistas,
        registrar_keywords=registrar_desde_items,
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fase B/C1: categoria_it con Gemini (solo nulls de keywords)"
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Camino directo: clasifica y muestra; no escribe")
    ap.add_argument("--proponer", action="store_true",
                    help="C1: dos pasadas Gemini -> artefacto JSON; no escribe")
    ap.add_argument("--aplicar", metavar="RUTA", default=None,
                    help="C1: aplica un artefacto --proponer o --consenso; no llama Gemini")
    ap.add_argument(
        "--consenso",
        nargs="+",
        metavar="ART",
        default=None,
        help="C1.5: cruza >=2 artefactos --proponer; no llama Gemini ni escribe BD",
    )
    ap.add_argument("--limit", type=int, default=0,
                    help="Tope de contratos (0 = todos del filtro)")
    ap.add_argument(
        "--filtro",
        choices=("vigentes", "evaluacion", "todos"),
        default="vigentes",
        help="Universo (default vigentes)",
    )
    ap.add_argument("--batch", type=int, default=30,
                    help="Tamano de lote Gemini P1 (default 30)")
    ap.add_argument(
        "--incluir-ventana-cerrada",
        action="store_true",
        default=False,
        help="Vigente sin filtrar fecha_fin_cotizacion (default off)",
    )
    ap.add_argument(
        "--max-llamadas-dia",
        type=int,
        default=MAX_LLAMADAS_DIA_DEFAULT,
        help=(
            f"Tope diario C4 (BD pipeline_cuota_c4; respaldo local "
            f"data/clasificacion_cuota.json). "
            f"Default {MAX_LLAMADAS_DIA_DEFAULT}; exit {EXIT_CUPO_C4}"
        ),
    )
    args = ap.parse_args()

    if args.max_llamadas_dia < 0:
        print("ERROR: --max-llamadas-dia debe ser >= 0", flush=True)
        return 1
    cfg = construir_config(args)

    if args.consenso is not None and len(args.consenso) < 2:
        print("ERROR: --consenso requiere al menos 2 artefactos", flush=True)
        return 2

    modos = [
        ("--proponer", bool(args.proponer)),
        ("--aplicar", bool(args.aplicar)),
        ("--consenso", args.consenso is not None),
        ("--dry-run", bool(args.dry_run)),
    ]
    activos = [n for n, on in modos if on]
    if len(activos) > 1:
        print(
            "ERROR: " + " y ".join(activos) + " son mutuamente excluyentes",
            flush=True,
        )
        return 2

    if args.consenso is not None:
        print("=" * 60, flush=True)
        print("C1 --consenso artefactos (sin Gemini, sin Supabase)", flush=True)
        print("=" * 60, flush=True)
        return comando_consenso(cfg, args.consenso)

    if args.aplicar:
        print("=" * 60, flush=True)
        print("C1 --aplicar artefacto (sin Gemini)", flush=True)
        print("=" * 60, flush=True)
        return comando_aplicar(cfg, args.aplicar)

    if not args.proponer:
        print(
            "[deprecado] usa --proponer / --aplicar / --consenso; "
            "el camino directo se elimina en C3",
            flush=True,
        )

    if args.batch <= 0:
        print("ERROR: --batch debe ser > 0", flush=True)
        return 1

    print("=" * 60, flush=True)
    print("Fase B -- clasificar categoria_it (Gemini)", flush=True)
    print(
        f"  proponer={args.proponer}  dry-run={args.dry_run}  "
        f"filtro={args.filtro}  limit={args.limit or 'all'}  "
        f"batch={args.batch}  incluir_ventana_cerrada="
        f"{args.incluir_ventana_cerrada}  max_llamadas_dia="
        f"{args.max_llamadas_dia}  modelo={GEMINI_FLASH}",
        flush=True,
    )
    print("=" * 60, flush=True)

    if not GEMINI_API_KEY:
        print("ERROR: GEMINI_API_KEY no encontrado", flush=True)
        return 1
    supa = init_supabase()
    if supa is None:
        return 1
    cfg.supa = supa

    print("SELECT sin fila en clasificacion_contrato...",
          flush=True)
    filas = paginar_nulls(
        supa,
        args.filtro,
        args.limit,
        incluir_ventana_cerrada=args.incluir_ventana_cerrada,
    )
    n_sel = len(filas)
    n_pobladas = sum(
        1 for r in filas
        if r.get("categoria_it") or r.get("relevancia_ia")
    )
    print(f"  candidatos={n_sel:,}  ya_poblados_en_lote={n_pobladas}",
          flush=True)
    if n_pobladas:
        print("ERROR: el SELECT trajo filas con etiqueta; aborto (cascada).",
              flush=True)
        return 1
    if not filas:
        print("Nada que clasificar.", flush=True)
        if args.proponer:
            return comando_proponer(
                cfg,
                filas=filas,
                filtro=args.filtro,
                limit=args.limit,
                batch=args.batch,
                incluir_ventana_cerrada=args.incluir_ventana_cerrada,
            )
        return 0

    if args.proponer:
        return comando_proponer(
            cfg,
            filas=filas,
            filtro=args.filtro,
            limit=args.limit,
            batch=args.batch,
            incluir_ventana_cerrada=args.incluir_ventana_cerrada,
        )
    return camino_directo(cfg, filas=filas, batch=args.batch, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
