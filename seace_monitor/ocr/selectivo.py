"""Corrida de OCR selectivo por página (Paso C).

Orquesta: cuota diaria → cola de pendientes → OCR por contrato → progreso y
resumen. Las dependencias operativas (cola, elegibilidad, persistencia, cliente
HTTP, reloj y salida) son inyectables para probar la corrida sin servicios.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from seace_monitor.config import RAIZ_REPO
from seace_monitor.documents.meta import columnas_extraccion_ok
from seace_monitor.documents.reportes import escribir_resumen
from seace_monitor.documents.seace_files import (
    MOTIVO_SIN_PDF,
    SeaceHttp,
    SinPdf,
    resumen_archivos,
)
from seace_monitor.ingestion.repository import (
    payload_rechazo,
    registrar_rechazo,
)
from seace_monitor.logging import PASO_OCR, registrar_run

from .cuota import cargar_cuota_ocr
from .gemini_provider import CupoFlash
from .pendientes import contrato_ocr_sigue_elegible, pendientes_ocr_paginas
from .queue import ocr_sin_margen_contrato, ocr_tiempo_agotado

OCR_LOG = RAIZ_REPO / "data" / "ultima_ocr.txt"
OCR_MAX_SEGUNDOS_DEFAULT = 7_200
USD_PEN = 3.75


def escribir_ocr_log(
    supa,
    stats: dict,
    *,
    log_path: Path | None = None,
) -> None:
    """Resumen del paso OCR: pipeline_runs + data/ultima_ocr.txt + summary."""
    log_path = log_path or OCR_LOG
    registrar_run(supa, PASO_OCR, stats)
    lines = [
        f"ts={datetime.now(timezone.utc).isoformat()}",
        *[f"{k}={v}" for k, v in stats.items()],
    ]
    texto = "\n".join(lines) + "\n"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(texto, encoding="utf-8")
    except OSError as e:
        print(f"  [warn] no se pudo escribir {log_path}: {e}", flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        md = (
            "### OCR selectivo\n\n"
            f"- cola vigente+ventana+TI: **{stats.get('cola_contratos')}** "
            f"contratos / **{stats.get('cola_paginas')}** páginas\n"
            f"- OCR-eados: **{stats.get('ocr_contratos')}** contratos / "
            f"**{stats.get('ocr_paginas')}** páginas\n"
            f"- flash: {stats.get('flash_hoy')}/{stats.get('max_dia')}\n"
            f"- **motivo_parada={stats.get('motivo_parada')}**  "
            f"elapsed={stats.get('elapsed_s')}s  exit=0\n"
        )
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(md)
        except OSError:
            pass
    print(texto, flush=True)


def run_ocr_selectivo(
    supa,
    *,
    ocr_contrato: Callable,
    rechunk: Callable,
    imprimir: Callable[[int, int, int, str, str], None],
    limit: int,
    ids: list[int],
    headed: bool,
    max_dia: int,
    dry_run: bool,
    solo_ti: bool = False,
    max_segundos: int = OCR_MAX_SEGUNDOS_DEFAULT,
    incluir_por_abrir: bool = False,
    rpm: float = 0.0,
    delay_s: float = 0.35,
    cargar_cuota: Callable = cargar_cuota_ocr,
    pendientes: Callable = pendientes_ocr_paginas,
    elegible: Callable = contrato_ocr_sigue_elegible,
    columnas_ok: Callable = columnas_extraccion_ok,
    http_factory: Callable = SeaceHttp,
    rechazar: Callable = registrar_rechazo,
    log_ocr: Callable = escribir_ocr_log,
    log_resumen: Callable = escribir_resumen,
    clock: Callable = time.monotonic,
    sleep: Callable = time.sleep,
) -> None:
    cuota = cargar_cuota(supa)
    t0 = clock()
    exigir_ti = solo_ti and not ids
    print("=" * 60, flush=True)
    print("PASO C — OCR selectivo por página (append, sin reemplazar nativo)", flush=True)
    print(
        f"  fecha_lima={cuota['fecha']}  usadas={cuota['requests']}/{max_dia}  "
        f"usd_est={cuota['usd_est']:.4f}  "
        f"S/{float(cuota['usd_est']) * USD_PEN:.2f}  "
        f"rpm={rpm or '-'}  "
        f"solo_ti={exigir_ti}  incluir_por_abrir={incluir_por_abrir}  "
        f"max_segundos={max_segundos or 'off'}",
        flush=True,
    )
    print(
        "  filtros: estado=Vigente AND fecha_fin_cotizacion IS NOT NULL "
        "AND fecha_fin_cotizacion > now() "
        + (
            "(incluye por-abrir)"
            if incluir_por_abrir
            else "AND (fecha_ini_cotizacion IS NULL OR fecha_ini_cotizacion <= now())"
        )
        + "  (NULL no gasta Flash)",
        flush=True,
    )
    if not columnas_ok(supa):
        print(
            "  [warn] columnas tdr_tipo_extraccion/paginas_ocr_* ausentes; "
            "la cola usa data/tdr_extraccion.jsonl "
            "(aplica sql/migraciones/tdr_extraccion_meta.sql + --sync-meta)",
            flush=True,
        )
    print("=" * 60, flush=True)

    def _log_final(
        motivo: str,
        *,
        ok_c: int = 0,
        pags_ok: int = 0,
        err: int = 0,
        cola_c: int = 0,
        cola_p: int = 0,
        ids_tocados: list[int] | None = None,
        cola_stats: dict | None = None,
        rest_c: int | None = None,
        rest_p: int | None = None,
    ) -> None:
        elapsed = int(clock() - t0)
        cs = cola_stats or {}
        stats = {
            "modo": "ocr_selectivo",
            "solo_ti": exigir_ti,
            "max_segundos": max_segundos,
            "cola_contratos": cola_c,
            "cola_paginas": cola_p,
            "cola_alta": cs.get("alta", ""),
            "cola_categoria_it": cs.get("categoria_it", ""),
            "cola_media": cs.get("media", ""),
            "cola_baja": cs.get("baja", ""),
            "descartados_no_ti": cs.get("no_ti", ""),
            "descartados_ventana_null": cs.get("ventana_null", ""),
            "descartados_vencidos": cs.get("vencidos", ""),
            "descartados_por_abrir": cs.get("por_abrir", ""),
            "ocr_contratos": ok_c,
            "ocr_paginas": pags_ok,
            "err": err,
            "flash_hoy": cuota["requests"],
            "max_dia": max_dia,
            "usd_est": round(float(cuota["usd_est"]), 6),
            "pen_est": round(float(cuota["usd_est"]) * USD_PEN, 4),
            "pendientes_contratos": rest_c if rest_c is not None else "",
            "pendientes_paginas": rest_p if rest_p is not None else "",
            "ids_tocados": ",".join(str(x) for x in (ids_tocados or [])),
            "motivo_parada": motivo,
            "elapsed_s": elapsed,
            "exit": 0,
        }
        print(f"\n{'=' * 60}", flush=True)
        print(
            f"OCR listo en {elapsed}s  contratos={ok_c} "
            f"paginas_ocr={pags_ok} err={err} "
            f"flash_hoy={cuota['requests']}/{max_dia}",
            flush=True,
        )
        print(f"  motivo_parada={motivo}  exit=0", flush=True)
        print("=" * 60, flush=True)
        log_ocr(supa, stats)
        log_resumen(supa, stats)

    if int(cuota["requests"]) >= max_dia:
        print("Tope diario ya alcanzado. Reanudar mañana.", flush=True)
        _log_final("cupo")
        return

    filas, cola_stats = pendientes(
        supa,
        10**9 if ids else limit,
        solo_ti=exigir_ti,
        exigir_ventana=True,
        incluir_por_abrir=incluir_por_abrir,
    )
    if ids:
        want = set(ids)
        filas = [r for r in filas if int(r["id"]) in want]

    cola_c = len(filas)
    cola_p = sum(len(r.get("paginas_ocr_pendientes") or []) for r in filas)
    print(
        f"  cola vigente+ventana+TI={cola_c} contratos  "
        f"paginas={cola_p}  "
        f"alta={cola_stats.get('alta')} categoria_it={cola_stats.get('categoria_it')} "
        f"media={cola_stats.get('media')} baja={cola_stats.get('baja')}",
        flush=True,
    )
    print(
        f"  descartados: no_ti={cola_stats.get('no_ti')} "
        f"ventana_null={cola_stats.get('ventana_null')} "
        f"vencidos={cola_stats.get('vencidos')} "
        f"por_abrir={cola_stats.get('por_abrir')} "
        f"no_vigente={cola_stats.get('no_vigente')}",
        flush=True,
    )
    for r in filas[:20]:
        print(
            f"    id={r['id']} ia={r.get('relevancia_ia') or '-'} "
            f"cat={r.get('categoria_it') or '-'} "
            f"ini={r.get('fecha_ini_cotizacion')} "
            f"fin={r.get('fecha_fin_cotizacion')} "
            f"tipo={r.get('tdr_tipo_extraccion')} "
            f"pend={len(r.get('paginas_ocr_pendientes') or [])}",
            flush=True,
        )
    if len(filas) > 20:
        print(f"    ... +{len(filas) - 20} contratos", flush=True)

    if dry_run:
        _log_final(
            "dry_run",
            cola_c=cola_c,
            cola_p=cola_p,
            cola_stats=cola_stats,
        )
        return
    if not filas:
        print("Nada que OCR-ear (idempotente).", flush=True)
        _log_final(
            "vacio",
            cola_c=cola_c,
            cola_p=cola_p,
            cola_stats=cola_stats,
            rest_c=0,
            rest_p=0,
        )
        return

    ok_c = 0
    pags_ok = 0
    err = 0
    ids_tocados: list[int] = []
    motivo_parada = "completo"
    http = http_factory(headed=headed)
    try:
        for i, c in enumerate(filas, 1):
            cid = int(c["id"])
            entro_ocr = False
            try:
                if ocr_tiempo_agotado(t0, max_segundos, clock=clock):
                    raise CupoFlash(
                        f"tope {max_segundos}s de reloj",
                        motivo="tiempo",
                    )
                if ocr_sin_margen_contrato(t0, max_segundos, clock=clock):
                    raise CupoFlash(
                        "quedan <45s; no arrancar contrato",
                        motivo="tiempo",
                    )
                ok_el, razon = elegible(
                    supa, cid, solo_ti=exigir_ti,
                    incluir_por_abrir=incluir_por_abrir,
                )
                if not ok_el:
                    print(f"  skip id={cid} {razon}", flush=True)
                    continue
                entro_ocr = True
                row = ocr_contrato(
                    http, supa, c, cuota, max_dia,
                    t0=t0, max_segundos=max_segundos,
                )
                nnew = len(row["nuevas"])
                pags_ok += nnew
                ok_c += 1
                if nnew:
                    ids_tocados.append(cid)
                    try:
                        rechunk(supa, cid)
                    except Exception as e:
                        print(
                            f"  [warn] rechunk/embed id={cid}: {e}",
                            flush=True,
                        )
                imprimir(
                    i, len(filas), cid, "OCR_OK",
                    f"nuevas={nnew} pend={row['pend']} "
                    f"flash={cuota['requests']}/{max_dia} "
                    f"S/{float(cuota['usd_est']) * USD_PEN:.2f}",
                )
            except CupoFlash as e:
                motivo_parada = getattr(e, "motivo", None) or "cupo"
                print(
                    f"  STOP OCR motivo_parada={motivo_parada}  {e}",
                    flush=True,
                )
                if entro_ocr:
                    if cid not in ids_tocados:
                        ids_tocados.append(cid)
                    try:
                        rechunk(supa, cid)
                    except Exception as e2:
                        print(
                            f"  [warn] rechunk/embed id={cid}: {e2}",
                            flush=True,
                        )
                break
            except SinPdf as e:
                err += 1
                imprimir(i, len(filas), cid, "SIN_PDF",
                         f"archivos={len(e.archivos)}")
                rechazar(
                    supa,
                    payload_rechazo(c, MOTIVO_SIN_PDF, {
                        "archivos": resumen_archivos(e.archivos),
                    }),
                    MOTIVO_SIN_PDF,
                    origen="pdf",
                )
            except Exception as e:
                err += 1
                imprimir(i, len(filas), cid, f"FAIL ({e})")
                rechazar(
                    supa,
                    payload_rechazo(c, str(e)[:500]),
                    str(e),
                    origen="pdf",
                )
            sleep(delay_s)
    finally:
        http.close()

    rest, _st = pendientes(
        supa, 10**9, solo_ti=exigir_ti, exigir_ventana=True
    )
    rest_p = sum(len(r.get("paginas_ocr_pendientes") or []) for r in rest)
    print(
        f"  pendientes restantes: contratos={len(rest)} paginas={rest_p}",
        flush=True,
    )
    _log_final(
        motivo_parada,
        ok_c=ok_c,
        pags_ok=pags_ok,
        err=err,
        cola_c=cola_c,
        cola_p=cola_p,
        ids_tocados=ids_tocados,
        cola_stats=cola_stats,
        rest_c=len(rest),
        rest_p=rest_p,
    )
