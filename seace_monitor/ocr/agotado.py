"""Dead-letter de la cola OCR (FIX-015).

Un contrato cuya página sigue fallando después de la contingencia
Novita→Gemini reintenta en cada corrida horaria y quema tokens de ambos
proveedores indefinidamente. Cuando acumula ``OCR_AGOTADO_UMBRAL``
rechazos dentro de la ventana móvil ``OCR_AGOTADO_VENTANA_H``, sus
``paginas_ocr_pendientes`` se vacían y queda auditado ``ocr_agotado``
con las páginas perdidas: sale de la cola, deja de gastar y la pérdida
es rastreable en ``ingesta_rechazados``.

La ventana móvil evita que rechazos históricos de causas ya corregidas
castiguen a un contrato sano: solo pesa el episodio actual. Un operador
puede devolverle intentos a un contrato marcando sus rechazos como
``resuelto`` — el conteo solo mira filas no resueltas.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from seace_monitor.documents.meta import registrar_meta_local
from seace_monitor.documents.pdf_extraction import chars_utiles
from seace_monitor.documents.repository import update_contrato
from seace_monitor.documents.seace_files import MOTIVO_SIN_PDF
from seace_monitor.ingestion.repository import (
    payload_rechazo,
    registrar_rechazo,
)

OCR_AGOTADO_UMBRAL = 8
OCR_AGOTADO_VENTANA_H = 72
MOTIVO_OCR_AGOTADO = "ocr_agotado"


def rechazos_ocr_recientes(
    supa,
    cid: int,
    *,
    horas: int = OCR_AGOTADO_VENTANA_H,
    now: datetime | None = None,
) -> int:
    """Rechazos de procesamiento no resueltos en la ventana móvil.

    Excluye ``sin archivo PDF`` (falla estructural sin gasto de IA) y
    cualquier fila marcada ``resuelto`` por un operador.
    """
    if supa is None:
        return 0
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=horas)).isoformat()
    try:
        res = (
            supa.table("ingesta_rechazados")
            .select("id", count="exact")
            .eq("id_contrato", int(cid))
            .eq("origen", "pdf")
            .eq("resuelto", False)
            .neq("motivo", MOTIVO_SIN_PDF)
            .gte("created_at", cutoff)
            .execute()
        )
    except Exception as e:
        print(f"  [warn] conteo rechazos id={cid}: {e}", flush=True)
        return 0
    if res.count is not None:
        return int(res.count)
    return len(res.data or [])


def agotar_ocr(
    supa,
    contrato: dict,
    *,
    rechazos: int = 0,
    meta_path: Path | None = None,
) -> None:
    """Saca el contrato de la cola y audita las páginas perdidas."""
    cid = int(contrato["id"])
    pend = list(contrato.get("paginas_ocr_pendientes") or [])
    hechas = list(contrato.get("paginas_ocr_hechas") or [])
    n_pag = int(contrato.get("tdr_n_paginas") or contrato.get("n_paginas") or 0)
    n_ocr = int(
        contrato.get("tdr_n_paginas_ocr")
        or contrato.get("n_paginas_ocr")
        or (len(pend) + len(hechas))
    )
    n_nat = int(
        contrato.get("tdr_n_paginas_nativas")
        or contrato.get("n_paginas_nativas")
        or max(n_pag - n_ocr, 0)
    )
    try:
        registrar_meta_local({
            "id": cid,
            "tdr_tipo_extraccion": contrato.get("tdr_tipo_extraccion"),
            "ocr_paginas": [],
            "ocr_hechas": hechas,
            "n_paginas": n_pag,
            "n_paginas_nativas": n_nat,
            "n_paginas_ocr": n_ocr,
            "chars_final": chars_utiles(contrato.get("tdr_texto") or ""),
        }, path=meta_path)
    except Exception as e:
        print(f"  [warn] meta local agotado id={cid}: {e}", flush=True)
    if supa is not None:
        update_contrato(supa, cid, {"paginas_ocr_pendientes": []})
    registrar_rechazo(
        supa,
        payload_rechazo(contrato, MOTIVO_OCR_AGOTADO, {
            "paginas_perdidas": pend,
            "paginas_hechas": hechas,
            "rechazos_ventana": rechazos,
            "umbral": OCR_AGOTADO_UMBRAL,
            "ventana_h": OCR_AGOTADO_VENTANA_H,
        }),
        MOTIVO_OCR_AGOTADO,
        origen="ocr",
    )
