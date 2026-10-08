"""Caso de uso para la descarga documental por lotes.

Mantiene fuera del entrypoint la selección de cola, el ciclo por contrato, la
clasificación de resultados y el cierre de la corrida. Los efectos costosos se
reciben como colaboradores para poder caracterizar el flujo sin red ni BD.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from seace_monitor.documents.meta import columnas_extraccion_ok, reporte_jsonl
from seace_monitor.documents.pdf_extraction import (
    NecesitaOcr,
    PdfExtractError,
    clasificar_tipo,
)
from seace_monitor.documents.persistencia import (
    guardar_ok,
    guardar_pdf_truncado,
    guardar_pendiente_ocr,
    guardar_sin_pdf,
    persistir_storage_si_hay,
)
from seace_monitor.documents.reportes import (
    conteo_pdf,
    escribir_resumen,
    reporte_extraccion,
)
from seace_monitor.documents.repository import contratos_por_ids, pendientes_pdf
from seace_monitor.documents.seace_files import (
    MOTIVO_NO_PDF,
    MOTIVO_PDF_TRUNCADO,
    MOTIVO_SIN_PDF,
    NoEsPdf,
    PdfTruncado,
    SeaceHttp,
    SinPdf,
    resumen_archivos,
)
from seace_monitor.ingestion.repository import payload_rechazo, registrar_rechazo


def ejecutar_descarga_batch(
    supa,
    *,
    ids: list[int],
    limit: int,
    modo: str,
    permitir_ocr: bool,
    dry_run: bool,
    headed: bool,
    compacto: bool,
    rpm: float,
    delay_s: float,
    temp_prefix: str,
    listar_url: str,
    descargar_url: str,
    gemini_habilitado: bool,
    procesar: Callable[..., dict],
    imprimir_linea: Callable[[int, int, int, str, str], None],
    imprimir_resultado: Callable[[int, int, dict, dict], None],
    imprimir_sin_pdf: Callable[[int, int, dict, list], None],
    http_factory: Callable[..., Any] = SeaceHttp,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    temp_dir: Callable[[], str] = tempfile.gettempdir,
) -> dict:
    """Ejecuta la cola PDF/TDR y devuelve el resumen persistido.

    ``procesar`` conserva el wrapper configurado por el entrypoint. Reloj,
    espera, cliente HTTP y ruta temporal son inyectables para pruebas.
    """
    if not dry_run and not columnas_extraccion_ok(supa):
        print(
            "  [warn] columnas de extracción ausentes; "
            "se guarda tdr_texto + jsonl. Corre sql/migraciones/tdr_extraccion_meta.sql "
            "y --sync-meta después.",
            flush=True,
        )

    counts = conteo_pdf(supa)
    filas = contratos_por_ids(supa, ids) if ids else pendientes_pdf(supa, limit, modo=modo)

    print("=" * 60, flush=True)
    print("Fase 3 — PDF/TDR (listar + descargar, httpx)", flush=True)
    print(
        f"  modo={modo}  dry-run={dry_run}  limit={limit}  "
        f"ids={ids or '-'}  cola={len(filas)}  rpm={rpm or '-'}",
        flush=True,
    )
    print(
        f"  vigentes={counts['vigentes']}  "
        f"pendientes={counts['pendientes']}  "
        f"ya_ok={counts['ya_descargados']}  "
        f"marcados_ocr={counts['pendiente_ocr']}",
        flush=True,
    )
    print(f"  LISTAR_URL={listar_url}", flush=True)
    print(f"  DESCARGAR_URL={descargar_url}", flush=True)
    print(f"  GEMINI_API_KEY set={gemini_habilitado}  (OCR fallback)", flush=True)
    print("=" * 60, flush=True)

    if not filas:
        print("Nada que hacer (cola vacia para este modo).", flush=True)
        jsonl = reporte_jsonl()
        print("\n--- PASO 1-bis jsonl ---", flush=True)
        for key, value in jsonl.items():
            print(f"  {key}={value}", flush=True)
        extra = dict(jsonl)
        if columnas_extraccion_ok(supa):
            tipos = reporte_extraccion(supa)
            print("\n--- PASO 1-bis vigentes BD ---", flush=True)
            for key, value in tipos.items():
                print(f"  {key}={value}", flush=True)
            extra.update(tipos)
        stats = {
            **counts,
            **extra,
            "ok": 0,
            "modo": modo,
            "limit": limit,
            "dry_run": dry_run,
            "elapsed_s": 0,
        }
        escribir_resumen(supa, stats)
        return stats

    ok = n_puro = n_mixto = n_imagen = 0
    ocr_paginas_total = skip_ocr = ocr_paginas_estimadas = 0
    sin_pdf = no_pdf = reintentados = truncados = err = 0
    started_at = now()
    leftovers_antes = {
        path.name for path in Path(temp_dir()).glob(f"{temp_prefix}*")
    }

    http = http_factory(headed=headed)
    try:
        for index, contrato in enumerate(filas, 1):
            contrato_id = int(contrato["id"])
            descripcion = (contrato.get("descripcion_contrato") or "")[:50]
            if (contrato.get("req_url") or "") == "sin_pdf":
                reintentados += 1
            try:
                row = procesar(
                    http,
                    contrato,
                    permitir_ocr=permitir_ocr,
                    supa=None if dry_run else supa,
                )
                tipo = row.get("tdr_tipo_extraccion") or clasificar_tipo(
                    int(row.get("n_paginas") or 0),
                    list(row.get("ocr_paginas") or []),
                )
                n_ocr = int(row.get("n_paginas_ocr") or len(row.get("ocr_paginas") or []))
                ocr_paginas_total += n_ocr
                if tipo == "nativo_puro":
                    n_puro += 1
                    etiqueta = "NATIVO_PURO"
                elif tipo == "mixto":
                    n_mixto += 1
                    etiqueta = "MIXTO"
                else:
                    n_imagen += 1
                    etiqueta = "IMAGEN_TOTAL"
                if compacto:
                    imprimir_linea(
                        index,
                        len(filas),
                        contrato_id,
                        etiqueta,
                        f"pags={row.get('n_paginas')} "
                        f"nat={row.get('n_paginas_nativas')} "
                        f"ocr={n_ocr} chars={row.get('chars_final')} "
                        f"acum_ocr={ocr_paginas_total}",
                    )
                else:
                    imprimir_resultado(index, len(filas), contrato, row)
                if not dry_run:
                    guardar_ok(supa, row)
                ok += 1
            except NecesitaOcr as error:
                skip_ocr += 1
                n_est = len(error.meta.get("ocr_paginas") or [])
                ocr_paginas_estimadas += n_est
                imprimir_linea(
                    index,
                    len(filas),
                    contrato_id,
                    "SKIP_OCR",
                    f"pags={error.meta.get('n_paginas')} ocr_pags={n_est} "
                    f"estim_acum={ocr_paginas_estimadas}",
                )
                if not dry_run:
                    guardar_pendiente_ocr(supa, contrato_id, error.meta)
            except SinPdf as error:
                sin_pdf += 1
                if compacto:
                    imprimir_linea(
                        index,
                        len(filas),
                        contrato_id,
                        "SIN_PDF",
                        f"archivos={len(error.archivos)}",
                    )
                else:
                    imprimir_sin_pdf(index, len(filas), contrato, error.archivos)
                if not dry_run:
                    guardar_sin_pdf(supa, contrato_id)
                    registrar_rechazo(
                        supa,
                        payload_rechazo(
                            contrato,
                            MOTIVO_SIN_PDF,
                            {"archivos": resumen_archivos(error.archivos)},
                        ),
                        MOTIVO_SIN_PDF,
                        origen="pdf",
                    )
            except NoEsPdf as error:
                no_pdf += 1
                imprimir_linea(index, len(filas), contrato_id, "NO_PDF", descripcion)
                if not dry_run:
                    guardar_sin_pdf(supa, contrato_id)
                    registrar_rechazo(
                        supa,
                        payload_rechazo(contrato, str(error)[:500]),
                        MOTIVO_NO_PDF,
                        origen="pdf",
                    )
            except PdfTruncado as error:
                truncados += 1
                imprimir_linea(
                    index,
                    len(filas),
                    contrato_id,
                    "PDF_TRUNCADO",
                    descripcion,
                )
                if not dry_run:
                    guardar_pdf_truncado(supa, contrato_id, contrato)
                    registrar_rechazo(
                        supa,
                        payload_rechazo(contrato, str(error)[:500]),
                        MOTIVO_PDF_TRUNCADO,
                        origen="pdf",
                    )
            except PdfExtractError as error:
                err += 1
                imprimir_linea(index, len(filas), contrato_id, f"FAIL ({error})", descripcion)
                if not dry_run:
                    persistir_storage_si_hay(supa, contrato_id, error.meta or {})
                    registrar_rechazo(
                        supa,
                        payload_rechazo(
                            contrato,
                            str(error)[:500],
                            {
                                "archivos": error.meta.get("archivos"),
                                "pdf_nombre": error.meta.get("pdf_nombre"),
                            },
                        ),
                        str(error),
                        origen="pdf",
                    )
            except Exception as error:
                err += 1
                imprimir_linea(index, len(filas), contrato_id, f"FAIL {error}", descripcion)
                if not dry_run:
                    registrar_rechazo(
                        supa,
                        payload_rechazo(contrato, str(error)[:500]),
                        str(error),
                        origen="pdf",
                    )
            if index % 20 == 0:
                elapsed = now() - started_at
                print(
                    f"  -- progreso {index}/{len(filas)}  "
                    f"puro={n_puro} mixto={n_mixto} imagen={n_imagen} "
                    f"pags_ocr={ocr_paginas_total} sin_pdf={sin_pdf} "
                    f"no_pdf={no_pdf} err={err} t={elapsed:.0f}s",
                    flush=True,
                )
            sleep(delay_s)
    finally:
        http.close()

    leftovers = [
        path.name
        for path in Path(temp_dir()).glob(f"{temp_prefix}*")
        if path.name not in leftovers_antes
    ]
    elapsed = now() - started_at
    counts_fin = conteo_pdf(supa)
    tipos = reporte_extraccion(supa) if columnas_extraccion_ok(supa) else {}
    print(f"\n{'=' * 60}", flush=True)
    print(
        f"Listo en {elapsed:.0f}s  ok={ok} "
        f"nativo_puro={n_puro} mixto={n_mixto} imagen_total={n_imagen} "
        f"pags_ocr_cola={ocr_paginas_total} "
        f"sin_pdf={sin_pdf} no_pdf={no_pdf} truncados={truncados} "
        f"reintentados={reintentados} err={err} dry-run={dry_run}",
        flush=True,
    )
    print(
        f"Vigentes={counts_fin['vigentes']}  "
        f"pendientes={counts_fin['pendientes']}  "
        f"marcados_ocr={counts_fin['pendiente_ocr']}  "
        f"ya_ok={counts_fin['ya_descargados']}",
        flush=True,
    )
    if tipos:
        print("\n--- PASO 1-bis vigentes (BD) ---", flush=True)
        for key in (
            "nativo_puro",
            "mixto",
            "imagen_total",
            "paginas_ocr_reales",
            "paginas_nativas",
            "paginas_totales",
            "sin_pdf",
            "pendiente_ocr_viejo",
            "sin_tipo",
        ):
            print(f"  {key}={tipos[key]}", flush=True)
    print(f"OCR_PAGINAS_REALES_COLA={ocr_paginas_total}", flush=True)
    print(
        f"Temps {temp_prefix}* residuales de esta corrida: "
        f"{leftovers if leftovers else 'ninguno (borrados)'}",
        flush=True,
    )
    print("=" * 60, flush=True)

    stats = {
        "modo": modo,
        "ok": ok,
        "nativo_puro_cola": n_puro,
        "mixto_cola": n_mixto,
        "imagen_total_cola": n_imagen,
        "ocr_paginas_cola": ocr_paginas_total,
        "skip_ocr": skip_ocr,
        "ocr_paginas_estimadas": ocr_paginas_estimadas,
        "sin_pdf_cola": sin_pdf,
        "pdf_truncado": truncados,
        "err": err,
        "limit": limit,
        "dry_run": dry_run,
        "elapsed_s": int(elapsed),
        "temps_residuales": ",".join(leftovers),
        **{f"fin_{key}": value for key, value in counts_fin.items()},
        **{f"bd_{key}": value for key, value in tipos.items()},
    }
    escribir_resumen(supa, stats)
    return stats
