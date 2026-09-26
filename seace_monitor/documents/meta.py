"""Metadatos de extracción TDR: sidecar local, columnas BD y sincronización.

El sidecar ``data/tdr_extraccion.jsonl`` es un registro reanudable
(append-only; la última línea por id gana) que conserva los metadatos de
extracción aunque las columnas ``tdr_*``/``paginas_ocr_*`` aún no existan en
PostgREST. ``sync_meta_jsonl`` vuelca el sidecar a la BD cuando el DDL ya está
aplicado, y reclasifica los huérfanos nativos que quedaron ``pdf_es_imagen``.
"""

from __future__ import annotations

import json
from pathlib import Path

from seace_monitor.config import RAIZ_REPO
from seace_monitor.documents.pdf_extraction import chars_utiles, clasificar_tipo
from seace_monitor.documents.repository import COLS_EXTRACCION, PAGE_DB
from seace_monitor.ocr.queue import aplanar_clasificacion

META_LOG = RAIZ_REPO / "data" / "tdr_extraccion.jsonl"
MIN_CHARS_PAGINA = 80


def registrar_meta_local(row: dict, *, path: Path | None = None) -> None:
    """Sidecar idempotente (última línea por id gana) por si aún no hay DDL."""
    path = path or META_LOG
    tipo = row.get("tdr_tipo_extraccion") or clasificar_tipo(
        int(row.get("n_paginas") or 0),
        list(row.get("ocr_paginas") or []),
    )
    rec = {
        "id": int(row["id"]),
        "tdr_tipo_extraccion": tipo,
        "paginas_ocr_pendientes": list(row.get("ocr_paginas") or []),
        "paginas_ocr_hechas": list(row.get("ocr_hechas") or []),
        "tdr_n_paginas": int(row.get("n_paginas") or 0),
        "tdr_n_paginas_nativas": int(row.get("n_paginas_nativas") or 0),
        "tdr_n_paginas_ocr": int(row.get("n_paginas_ocr") or 0),
        "pdf_es_imagen": tipo != "nativo_puro",
        "chars_final": int(row.get("chars_final") or 0),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def meta_local_por_id(*, path: Path | None = None) -> dict[int, dict]:
    path = path or META_LOG
    by_id: dict[int, dict] = {}
    if not path.exists():
        return by_id
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        by_id[int(rec["id"])] = rec
    return by_id


def reporte_jsonl(*, path: Path | None = None) -> dict:
    path = path or META_LOG
    if not path.exists():
        return {
            "nativo_puro": 0, "mixto": 0, "imagen_total": 0,
            "paginas_ocr_reales": 0, "paginas_nativas": 0, "paginas_totales": 0,
            "contratos": 0,
        }
    by_id = meta_local_por_id(path=path)
    nativo = mixto = imagen = 0
    pags_ocr = pags_nat = pags_tot = 0
    for rec in by_id.values():
        tipo = rec.get("tdr_tipo_extraccion")
        if tipo == "nativo_puro":
            nativo += 1
        elif tipo == "mixto":
            mixto += 1
        elif tipo == "imagen_total":
            imagen += 1
        pags_ocr += int(rec.get("tdr_n_paginas_ocr") or 0)
        pags_nat += int(rec.get("tdr_n_paginas_nativas") or 0)
        pags_tot += int(rec.get("tdr_n_paginas") or 0)
    return {
        "nativo_puro": nativo,
        "mixto": mixto,
        "imagen_total": imagen,
        "paginas_ocr_reales": pags_ocr,
        "paginas_nativas": pags_nat,
        "paginas_totales": pags_tot,
        "contratos": len(by_id),
    }


def payload_extraccion(rec: dict) -> dict:
    tipo = rec.get("tdr_tipo_extraccion")
    pend = rec.get("paginas_ocr_pendientes")
    if pend is None:
        pend = rec.get("ocr_paginas") or []
    hechas = rec.get("paginas_ocr_hechas")
    if hechas is None:
        hechas = rec.get("ocr_hechas") or []
    pdf_img = rec.get("pdf_es_imagen")
    if pdf_img is None:
        pdf_img = tipo != "nativo_puro" if tipo else None
    return {
        "pdf_es_imagen": pdf_img,
        "tdr_tipo_extraccion": tipo,
        "paginas_ocr_pendientes": list(pend or []),
        "paginas_ocr_hechas": list(hechas or []),
        "tdr_n_paginas": rec.get("tdr_n_paginas") or rec.get("n_paginas"),
        "tdr_n_paginas_nativas": rec.get("tdr_n_paginas_nativas")
        or rec.get("n_paginas_nativas"),
        "tdr_n_paginas_ocr": rec.get("tdr_n_paginas_ocr")
        or rec.get("n_paginas_ocr"),
    }


def columnas_extraccion_ok(supa) -> bool:
    """True si PostgREST ya reconoce las columnas de extracción."""
    try:
        (
            supa.table("contratos")
            .select(",".join(COLS_EXTRACCION))
            .limit(1)
            .execute()
        )
        return True
    except Exception:
        return False


def update_extraccion(supa, cid: int, payload: dict) -> None:
    """Escribe columnas de extracción sin fallback silencioso.

    A diferencia de ``update_contrato`` (que reintenta "slim" sin las columnas
    nuevas), aquí un fallo sí debe romper: el sync no puede dejar mixto/imagen
    en NULL por un schema cache viejo de PostgREST.
    """
    try:
        supa.table("contratos").update(payload).eq("id", cid).execute()
    except Exception as e:
        raise RuntimeError(f"sync id={cid} falló: {e}") from e


def sync_meta_jsonl(
    supa,
    *,
    path: Path | None = None,
    min_chars: int = MIN_CHARS_PAGINA,
) -> int:
    path = path or META_LOG
    if not columnas_extraccion_ok(supa):
        raise SystemExit(
            "ERROR: faltan columnas. Ejecuta sql/migraciones/tdr_extraccion_meta.sql "
            "(y NOTIFY pgrst, 'reload schema') y reintenta --sync-meta"
        )
    if not path.exists():
        print("  [sync] no hay data/tdr_extraccion.jsonl", flush=True)
        return 0
    by_id = meta_local_por_id(path=path)
    print(
        f"  [sync] jsonl ids={len(by_id)}  "
        + " ".join(
            f"{k}={sum(1 for r in by_id.values() if r.get('tdr_tipo_extraccion')==k)}"
            for k in ("nativo_puro", "mixto", "imagen_total")
        ),
        flush=True,
    )
    probe_id, probe_rec = next(iter(by_id.items()))
    probe_payload = payload_extraccion(probe_rec)
    update_extraccion(supa, probe_id, probe_payload)
    check = (
        supa.table("contratos")
        .select("tdr_tipo_extraccion")
        .eq("id", probe_id)
        .limit(1)
        .execute()
    )
    got = (check.data or [{}])[0].get("tdr_tipo_extraccion")
    want = probe_payload.get("tdr_tipo_extraccion")
    if got != want:
        raise SystemExit(
            f"ERROR: PostgREST no persistió tdr_tipo_extraccion "
            f"(id={probe_id} escribió={want!r} leyó={got!r}). "
            f"En SQL Editor: NOTIFY pgrst, 'reload schema'; y reintenta."
        )
    print(f"  [sync] probe id={probe_id} tipo={got} OK", flush=True)

    n = 0
    err = 0
    for cid, rec in by_id.items():
        if cid == probe_id:
            n += 1
            continue
        try:
            update_extraccion(supa, cid, payload_extraccion(rec))
            n += 1
        except Exception as e:
            err += 1
            print(f"  [sync] FAIL id={cid}: {e}", flush=True)
            if err >= 5:
                raise SystemExit("ERROR: demasiados fallos de sync; aborto")
        if n % 200 == 0:
            print(f"  [sync] {n}/{len(by_id)}", flush=True)
    print(f"  [sync] jsonl escritos={n} err={err}", flush=True)

    # Gemelos con texto nativo que quedaron pdf_es_imagen=true sin sidecar.
    n_nat = 0
    offset = 0
    while True:
        res = (
            supa.table("contratos")
            .select("id,tdr_texto,pdf_es_imagen,tdr_tipo_extraccion,req_url")
            .eq("estado", "Vigente")
            .eq("pdf_es_imagen", True)
            .is_("tdr_tipo_extraccion", "null")
            # PostgREST: .range() sin .order() no garantiza orden entre paginas;
            # la pagina 2 puede repetir filas de la 1 y omitir otras.
            .order("id")
            .range(offset, offset + PAGE_DB - 1)
            .execute()
        )
        batch = res.data or []
        for r in batch:
            aplanar_clasificacion(r)
            cid = int(r["id"])
            if cid in by_id:
                continue
            tdr = r.get("tdr_texto") or ""
            if (r.get("req_url") or "") == "sin_pdf":
                continue
            if "(ocr)" in tdr or chars_utiles(tdr) < min_chars:
                print(
                    f"  [sync] huerfano id={cid} no clasificado "
                    f"(chars={chars_utiles(tdr)})",
                    flush=True,
                )
                continue
            update_extraccion(supa, cid, {
                "tdr_tipo_extraccion": "nativo_puro",
                "pdf_es_imagen": False,
                "paginas_ocr_pendientes": [],
                "paginas_ocr_hechas": [],
                "tdr_n_paginas_ocr": 0,
            })
            n_nat += 1
            print(f"  [sync] huerfano id={cid} → nativo_puro", flush=True)
        if len(batch) < PAGE_DB:
            break
        offset += PAGE_DB
    if n_nat:
        print(f"  [sync] huerfanos nativo_puro={n_nat}", flush=True)
    return n + n_nat
