#!/usr/bin/env python3
"""
Fase 3.5 — Extraer TDR de contenedores no-PDF (vigentes TI/IA).

Cubre los anexos que NO son PDF y que el pipeline de PDF (`descargar_requerimiento`)
descarta por no tener magic `%PDF`: DOCX, ZIP (con hijos PDF/DOCX/XLSX/imagen) y,
si hay binario `unrar`/`7z`, RAR. El `.doc` binario (OLE2) se deja fuera por ahora.

Estrategia (híbrida, aprobada 19 sep):
  - DOCX  → python-docx (párrafos + tablas).
  - ZIP   → zipfile en memoria (sin extraer a disco; inmune a path traversal),
            clasifica cada hijo por magic bytes y extrae pdf/docx/xlsx/imagen.
  - RAR   → rarfile solo si hay backend; si no, rechazo `rar_sin_binario`.
  - .doc  → pospuesto (rechazo `doc_sin_soporte`).

El resultado confluye al flujo existente: escribe `contratos.tdr_texto` +
`tdr_tipo_extraccion=contenedor_*`, y dispara rechunk + embed (igual que el OCR).

Alcance: SOLO vigentes TI/IA sin TDR (coherente con el resto del pipeline).

Uso:
  uv run python extraer_contenedores.py --dry-run
  uv run python extraer_contenedores.py --limit 10
  uv run python extraer_contenedores.py --ids 6574,41042
"""
from __future__ import annotations

from seace_monitor.config import cargar_env

import argparse
import hashlib
import io
import os
import re
import tempfile
import time
from collections import Counter
from pathlib import Path

import pymupdf
import httpx
from seace_monitor.supabase_client import crear_cliente

from seace_monitor.documents.pdf_extraction import chars_utiles, limpiar_texto
from seace_monitor.documents.postprocess import rechunk_embed_pdf as _rechunk_embed_pdf
from seace_monitor.documents.seace_files import (
    DEFAULT_DESCARGAR_URL,
    DEFAULT_LISTAR_URL,
    SeaceHttp,
    elegir_pdf,
    listar_archivos as listar_archivos_seace,
    resumen_archivos,
)
from seace_monitor.gemini import usd_flash as usd_de_tokens
from seace_monitor.ocr.gemini_provider import (
    GEMINI_FLASH,
    OCR_USAGE_ACUM,
    solicitar_ocr_gemini,
)
from seace_monitor.ocr.queue import aplanar_clasificacion as _aplanar_cl
from seace_monitor.ingestion.repository import registrar_rechazo
from seace_monitor.logging import PASO_CONTENEDORES, registrar_evento, registrar_run

# ── Cargar .env ────────────────────────────────────────────────────────────────
cargar_env()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
LISTAR_URL = os.environ.get("LISTAR_URL", DEFAULT_LISTAR_URL).strip()
DESCARGAR_URL = os.environ.get("DESCARGAR_URL", DEFAULT_DESCARGAR_URL).strip()
DELAY_S = 0.35

PAGE_DB = 1_000
# Guardas de seguridad: no inflar memoria/tiempo con contenedores hostiles.
MAX_ARCHIVOS_ZIP = 100          # nº de hijos dentro de un zip
MAX_BYTES_ZIP = 50 * 1024 * 1024  # tamaño descomprimido acumulado (50 MB)
MAX_IMG_OCR = 5                 # máx imágenes OCR por contrato (ahorra Flash)
MIN_CHARS_UTIL = 200            # umbral para considerar texto extraído útil
_TZ_NOMBRE_IGNORABLE = re.compile(
    r"(?i)(^|/)(__MACOSX|\.DS_Store|Thumbs\.db|~\\$.*\.doc[x]?)$"
)
_EXTS_IMG = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"}

MOTIVO_RAR = "contenedor rar sin binario (unrar/7z)"
MOTIVO_DOC = "contenedor doc binario (OLE2) sin soporte"
MOTIVO_DESC = "contenedor no procesable"


def parse_ids(raw: str) -> list[int]:
    return [int(part) for part in re.split(r"[,\s]+", raw.strip()) if part]


def listar_archivos(http: SeaceHttp, cid: int) -> tuple[str, list]:
    return listar_archivos_seace(http, cid, LISTAR_URL)


def ocr_pagina_gemini(img_bytes: bytes, mime: str = "image/jpeg") -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY ausente; no se puede hacer OCR")
    return limpiar_texto(
        solicitar_ocr_gemini(httpx, img_bytes, mime, GEMINI_API_KEY)
    )


def rechunk_embed_pdf(supa, cid: int) -> None:
    _rechunk_embed_pdf(supa, cid, api_key=GEMINI_API_KEY)


# ── Detección por magic bytes ─────────────────────────────────────────────────
def _head(body: bytes, n: int = 8) -> bytes:
    return (body or b"")[:n]


def _es_zip(body: bytes) -> bool:
    h = _head(body, 4)
    return h == b"PK\x03\x04" or h == b"PK\x05\x06" or h == b"PK\x07\x08"


def _es_rar(body: bytes) -> bool:
    h = _head(body, 8)
    return h.startswith(b"Rar!\x1a\x07\x00") or h.startswith(b"Rar!\x1a\x07\x01")


def _es_ole2(body: bytes) -> bool:
    return _head(body, 8) == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _zip_kind(body: bytes) -> str:
    """Distingue docx/xlsx/pptx/zip genérico por su estructura interna."""
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            names = set(zf.namelist())
    except Exception:
        return "zip"
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    if "ppt/presentation.xml" in names:
        return "pptx"
    return "zip"


def tipo_contenedor(body: bytes, nombre: str = "") -> str:
    """Tipo real por magic bytes (el mime/extensión de SEACE NO es fiable)."""
    b = body or b""
    if not b:
        return "vacio"
    if b.lstrip().startswith(b"%PDF"):
        return "pdf"
    if _es_rar(b):
        return "rar"
    if _es_ole2(b):
        return "ole2"
    if _es_zip(b):
        return _zip_kind(b)
    ext = (nombre or "").lower().rsplit(".", 1)[-1] if "." in (nombre or "") else ""
    if ext in _EXTS_IMG:
        return "imagen"
    if b[:4] == b"\x89PNG":
        return "imagen"
    if b[:2] == b"\xff\xd8":
        return "imagen"
    return "desconocido"


def _mime_imagen(body: bytes) -> str:
    if body[:4] == b"\x89PNG":
        return "image/png"
    if body[:2] == b"\xff\xd8":
        return "image/jpeg"
    return "image/jpeg"


# ── Extractores por formato ───────────────────────────────────────────────────
def _docx_texto_nativo(body: bytes) -> str:
    """Texto de word/document.xml: TODO `w:p` en orden (incluye tablas y cajas)."""
    import zipfile

    from lxml import etree

    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        xml = zf.read("word/document.xml")
    root = etree.fromstring(xml)
    lineas: list[str] = []
    for p in root.iter(f"{{{W}}}p"):
        trozos = [t.text or "" for t in p.iter(f"{{{W}}}t")]
        linea = "".join(trozos).strip()
        if linea:
            lineas.append(linea)
    return limpiar_texto("\n".join(lineas))


def _docx_imagenes(body: bytes) -> list[bytes]:
    """Imágenes embebidas (word/media/*) en orden numérico. Escaneos DOCX."""
    import zipfile

    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        nombres = [n for n in zf.namelist() if n.lower().startswith("word/media/")]
        nombres.sort(key=lambda n: _num_img(n))
        out: list[bytes] = []
        for n in nombres:
            data = zf.read(n)
            if data:
                out.append(data)
    return out


def _num_img(nombre: str) -> int:
    m = re.search(r"(\d+)", nombre.rsplit("/", 1)[-1])
    return int(m.group(1)) if m else 0


def extraer_docx(body: bytes) -> tuple[str, dict]:
    """Texto de un DOCX. Si es escaneado (sin texto nativo, con imágenes),
    hace OCR de las imágenes embebidas (tope MAX_IMG_OCR)."""
    stats: dict = {"nativo_chars": 0, "imagenes": 0, "ocr_img": 0}
    nativo = _docx_texto_nativo(body)
    stats["nativo_chars"] = chars_utiles(nativo)
    if stats["nativo_chars"] >= MIN_CHARS_UTIL:
        return nativo, stats
    imgs = _docx_imagenes(body)
    stats["imagenes"] = len(imgs)
    if not imgs or not GEMINI_API_KEY:
        return nativo, stats
    bloques = [nativo] if nativo.strip() else []
    for i, img in enumerate(imgs, 1):
        if stats["ocr_img"] >= MAX_IMG_OCR:
            break
        try:
            ocr = ocr_pagina_gemini(img, _mime_imagen(img))
            bloques.append(f"--- pagina {i} (ocr) ---\n{ocr.strip()}")
            stats["ocr_img"] += 1
        except Exception:
            continue
    return limpiar_texto("\n\n".join(bloques)), stats


def extraer_xlsx(body: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(body), read_only=True, data_only=True)
    out: list[str] = []
    for ws in wb.worksheets:
        out.append(f"--- hoja {ws.title} ---")
        for row in ws.iter_rows(values_only=True):
            celdas = ["" if v is None else str(v).strip() for v in row]
            if any(celdas):
                out.append(" | ".join(celdas))
    wb.close()
    return limpiar_texto("\n".join(out))


def extraer_pdf_bytes(body: bytes) -> str:
    with pymupdf.open(stream=body, filetype="pdf") as doc:
        paginas = [(page.get_text("text") or "").strip() for page in doc]
    return limpiar_texto("\n".join(p for p in paginas if p))


def _nombre_ignorable(nombre: str) -> bool:
    base = nombre.replace("\\", "/").split("/")[-1]
    if base in (".DS_Store", "Thumbs.db"):
        return True
    if base.startswith("~$"):
        return True
    return bool(_TZ_NOMBRE_IGNORABLE.search(nombre))


def extraer_zip(body: bytes) -> tuple[str, dict]:
    """Extrae texto de todos los hijos procesables de un ZIP (en memoria).

    Devuelve (texto, stats). No escribe a disco: inmune a path traversal.
    """
    import zipfile

    stats: dict = {"archivos": 0, "por_tipo": Counter(), "ocr_img": 0, "omitidos": 0}
    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
    except Exception as e:
        raise RuntimeError(f"zip ilegible: {e}") from e

    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ARCHIVOS_ZIP:
            raise RuntimeError(f"zip con {len(infos)} archivos (> {MAX_ARCHIVOS_ZIP})")
        total_bytes = sum(i.file_size for i in infos)
        if total_bytes > MAX_BYTES_ZIP:
            raise RuntimeError(
                f"zip descomprimido {total_bytes} bytes (> {MAX_BYTES_ZIP})"
            )
        infos.sort(key=lambda i: i.filename.lower())
        bloques: list[str] = []
        stats["archivos"] = len(infos)
        for info in infos:
            nombre = info.filename
            if _nombre_ignorable(nombre):
                stats["omitidos"] += 1
                continue
            try:
                data = zf.read(info)
            except Exception:
                stats["omitidos"] += 1
                continue
            if not data:
                continue
            tipo = tipo_contenedor(data, nombre)
            stats["por_tipo"][tipo] += 1
            texto = ""
            try:
                if tipo == "pdf":
                    texto = extraer_pdf_bytes(data)
                elif tipo == "docx":
                    texto, _s = extraer_docx(data)
                elif tipo == "xlsx":
                    texto = extraer_xlsx(data)
                elif tipo == "imagen":
                    if GEMINI_API_KEY and stats["ocr_img"] < MAX_IMG_OCR:
                        texto = ocr_pagina_gemini(data, _mime_imagen(data))
                        stats["ocr_img"] += 1
                # rar anidado / ole2 / pptx / desconocido: se omiten (silencioso)
            except Exception:
                texto = ""
            if (texto or "").strip():
                bloques.append(f"--- {nombre} ---\n{texto.strip()}")
    texto = limpiar_texto("\n\n".join(bloques))
    return texto, stats


_RAR_TOOLS = ("unrar", "unrar-free", "7z", "7za", "7zz", "bsdtar", "unar")


def _find_rar_tool() -> str | None:
    import shutil

    for t in _RAR_TOOLS:
        if shutil.which(t):
            return t
    return None


def extraer_rar(body: bytes) -> tuple[str, dict]:
    """Extrae texto de un RAR vía rarfile (requiere binario unrar/7z/bsdtar).

    Lanza RuntimeError si no hay backend. El caller decide si registrar rechazo.
    """
    import rarfile

    tool = _find_rar_tool()
    if not tool:
        raise RuntimeError(MOTIVO_RAR)
    rarfile.UNRAR_TOOL = tool
    fd, tmp_name = tempfile.mkstemp(prefix="seace-rar-", suffix=".rar")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_bytes(body)
        rf = rarfile.RarFile(str(tmp))
        infos = [i for i in rf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ARCHIVOS_ZIP:
            raise RuntimeError(f"rar con {len(infos)} archivos (> {MAX_ARCHIVOS_ZIP})")
        bloques: list[str] = []
        stats: dict = {"archivos": len(infos), "por_tipo": Counter(), "ocr_img": 0}
        for info in sorted(infos, key=lambda i: i.filename.lower()):
            if _nombre_ignorable(info.filename):
                continue
            data = rf.read(info.filename)
            if not data:
                continue
            tipo = tipo_contenedor(data, info.filename)
            stats["por_tipo"][tipo] += 1
            texto = ""
            try:
                if tipo == "pdf":
                    texto = extraer_pdf_bytes(data)
                elif tipo == "docx":
                    texto, _s = extraer_docx(data)
                elif tipo == "xlsx":
                    texto = extraer_xlsx(data)
                elif tipo == "imagen":
                    if GEMINI_API_KEY and stats["ocr_img"] < MAX_IMG_OCR:
                        texto = ocr_pagina_gemini(data, _mime_imagen(data))
                        stats["ocr_img"] += 1
            except Exception:
                texto = ""
            if (texto or "").strip():
                bloques.append(f"--- {info.filename} ---\n{texto.strip()}")
        return limpiar_texto("\n\n".join(bloques)), stats
    finally:
        tmp.unlink(missing_ok=True)


# ── Elección de anexo contenedor ──────────────────────────────────────────────
_PRIORIDAD_EXT = {"docx": 0, "zip": 1, "rar": 2, "doc": 3}


def _ext(nombre: str) -> str:
    n = (nombre or "").lower().strip()
    return n.rsplit(".", 1)[-1] if "." in n else ""


def elegir_contenedor(archivos: list) -> dict | None:
    """Elige el mejor anexo NO-PDF. Prefiere docx, luego zip, rar, doc.

    No compite con `elegir_pdf`: este script corre sobre contratos donde ya
    se descartó el PDF (o no hay). Devuelve None si no hay contenedor.
    """
    cands: list[tuple[int, dict]] = []
    for a in archivos:
        if not isinstance(a, dict):
            continue
        nombre = str(a.get("nombre") or "")
        mime = str(a.get("descripcionMime") or "").lower()
        ext = _ext(nombre)
        if ext in _PRIORIDAD_EXT:
            cands.append((_PRIORIDAD_EXT[ext], a))
        elif "word" in mime or "msword" in mime:
            cands.append((0, a))
        elif "zip" in mime or "zip" in nombre.lower():
            cands.append((1, a))
        elif "rar" in mime or "rar" in nombre.lower():
            cands.append((2, a))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0][1]


# ── Descarga cruda (sin validación PDF) ───────────────────────────────────────
def _parece_html(body: bytes) -> bool:
    sample = (body or b"")[:400].lstrip().lower()
    return (
        sample.startswith(b"<!doctype")
        or sample.startswith(b"<html")
        or b"<html" in sample[:200]
    )


def descargar_crudo(http: SeaceHttp, url: str) -> bytes:
    status, headers, body = http.get_bytes(url)
    ctype = headers.get("content-type") or ""
    if status != 200:
        raise RuntimeError(f"descargar HTTP {status} ({ctype[:80]})")
    if not body:
        raise RuntimeError("respuesta vacía al descargar contenedor")
    if _parece_html(body) or "json" in ctype.lower() or "text/html" in ctype.lower():
        raise RuntimeError(f"binario parece HTML/JSON (content-type={ctype[:80]})")
    return body


# ── Selección de vigentes TI/IA sin TDR ───────────────────────────────────────
def vigentes_ti_sin_tdr(supa, limit: int) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while len(out) < limit:
        take = min(PAGE_DB, limit - len(out))
        res = (
            supa.table("contratos")
            .select(
                "id,nro_contratacion,descripcion_contrato,entidad,"
                "fecha_publica,pdf_descargado,req_url,tdr_texto,"
                "clasificacion_contrato(categoria_it,relevancia_ia)"
            )
            .eq("estado", "Vigente")
            .is_("tdr_texto", "null")
            .order("id", desc=True)
            .range(offset, offset + take - 1)
            .execute()
        )
        batch = res.data or []
        for r in batch:
            _aplanar_cl(r)
            if r.get("categoria_it") or r.get("relevancia_ia"):
                out.append(r)
        if len(batch) < take:
            break
        offset += take
    return out[:limit]


# ── Escritura ─────────────────────────────────────────────────────────────────
def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def guardar_texto_contenedor(supa, cid: int, texto: str, meta: dict) -> None:
    supa.table("contratos").update({
        "tdr_texto": texto or None,
        "pdf_hash": meta.get("hash"),
        "pdf_es_imagen": False,
        "pdf_descargado": True,
        "pdf_procesado": bool(texto and chars_utiles(texto) >= MIN_CHARS_UTIL),
        "req_url": (meta.get("url") or "")[:2000],
        "tdr_tipo_extraccion": meta.get("tipo_extraccion"),
        "paginas_ocr_pendientes": [],
        "paginas_ocr_hechas": [],
        "tdr_n_paginas": None,
        "tdr_n_paginas_nativas": None,
        "tdr_n_paginas_ocr": None,
        "pdf_archivo_id": meta.get("aid"),
        "pdf_nombre": (meta.get("nombre") or "")[:500] or None,
    }).eq("id", cid).execute()


def _payload_rechazo(c: dict, motivo: str, extra: dict | None = None) -> dict:
    out = {
        "idContrato": int(c["id"]),
        "nroContratacion": c.get("nro_contratacion"),
        "desContratacion": c.get("descripcion_contrato"),
        "motivo": motivo,
    }
    if extra:
        out.update(extra)
    return out


# ── Procesamiento por contrato ────────────────────────────────────────────────
def procesar_contenedor(http: SeaceHttp, supa, c: dict, dry_run: bool) -> str:
    """Intenta extraer el TDR del contenedor no-PDF. Devuelve un estado corto."""
    cid = int(c["id"])
    # Reset del acumulador OCR para trazar solo este contrato.
    OCR_USAGE_ACUM.update({"prompt": 0, "candidates": 0, "total": 0, "llamadas": 0})
    _url, archivos = listar_archivos(http, cid)
    es_sin_pdf = (c.get("req_url") or "") == "sin_pdf"

    elegido = elegir_contenedor(archivos)
    if elegido is None:
        # ¿"PDF" disfrazado? Solo si el pipeline PDF ya lo rechazó (req_url=sin_pdf)
        # y el anexo que "parece PDF" es en realidad docx/zip/rar. Lo descargamos
        # y el tipo real se decide por magic bytes más abajo.
        pdf = elegir_pdf(archivos)
        if pdf is not None and es_sin_pdf:
            elegido = pdf
        else:
            return "tiene_pdf" if pdf is not None else "sin_contenedor"

    aid = elegido.get("idContratoArchivo")
    if not aid:
        return "sin_aid"
    dl_url = DESCARGAR_URL.format(idContratoArchivo=aid, id=aid, id_archivo=aid)
    nombre = elegido.get("nombre")

    try:
        body = descargar_crudo(http, dl_url)
    except Exception as e:
        if not dry_run:
            registrar_rechazo(
                supa,
                _payload_rechazo(c, str(e)[:500], {"archivos": resumen_archivos(archivos)}),
                str(e),
                origen="contenedor",
            )
        return f"descarga_fallo({str(e)[:40]})"

    tipo = tipo_contenedor(body, nombre or "")
    try:
        if tipo == "docx":
            texto, _stats = extraer_docx(body)
        elif tipo == "zip":
            texto, _stats = extraer_zip(body)
        elif tipo == "rar":
            texto, _stats = extraer_rar(body)
        elif tipo == "pdf":
            # El mime/nombre mentía; era un PDF. Lo dejamos al pipeline PDF.
            return "es_pdf"
        elif tipo == "ole2":
            if not dry_run:
                registrar_rechazo(
                    supa,
                    _payload_rechazo(c, MOTIVO_DOC, {"nombre": nombre}),
                    MOTIVO_DOC,
                    origen="contenedor",
                )
            return "doc_sin_soporte"
        else:
            if not dry_run:
                registrar_rechazo(
                    supa,
                    _payload_rechazo(c, MOTIVO_DESC, {"nombre": nombre, "tipo": tipo}),
                    MOTIVO_DESC,
                    origen="contenedor",
                )
            return f"tipo_{tipo}"
    except Exception as e:
        motivo = str(e)
        if "rar sin binario" in motivo or "rar" in tipo:
            motivo = MOTIVO_RAR
        if not dry_run:
            registrar_rechazo(
                supa,
                _payload_rechazo(c, motivo[:500], {"nombre": nombre, "tipo": tipo}),
                motivo[:500],
                origen="contenedor",
            )
        return f"extraccion_fallo({motivo[:40]})"

    if chars_utiles(texto) < MIN_CHARS_UTIL:
        if not dry_run:
            registrar_rechazo(
                supa,
                _payload_rechazo(
                    c, "contenedor extraído sin texto útil",
                    {"nombre": nombre, "tipo": tipo, "chars": chars_utiles(texto)},
                ),
                "contenedor extraído sin texto útil",
                origen="contenedor",
            )
        return f"texto_vacio({chars_utiles(texto)})"

    meta = {
        "hash": _sha256(body),
        "url": dl_url,
        "aid": int(aid),
        "nombre": nombre,
        "tipo_extraccion": f"contenedor_{tipo}",
    }
    if not dry_run:
        guardar_texto_contenedor(supa, cid, texto, meta)
        registrar_evento(
            supa,
            cid,
            "contenedor",
            chars_tdr=chars_utiles(texto),
            tipo_extraccion=f"contenedor_{tipo}",
            detalle={"nombre": nombre, "archivos": resumen_archivos(archivos)},
        )
        # OCR embebido (imágenes de DOCX/ZIP/RAR): traza a uso_ia.
        if OCR_USAGE_ACUM.get("llamadas", 0) > 0:
            try:
                supa.table("uso_ia").insert({
                    "componente": "ocr",
                    "modelo": GEMINI_FLASH,
                    "tokens_prompt": int(OCR_USAGE_ACUM["prompt"]),
                    "tokens_completion": int(OCR_USAGE_ACUM["candidates"]),
                    "tokens_total": int(OCR_USAGE_ACUM["total"]),
                    "costo_usd": usd_de_tokens(
                        int(OCR_USAGE_ACUM["prompt"]),
                        int(OCR_USAGE_ACUM["candidates"]),
                    ),
                    "cache_hit": False,
                    "detalle": {
                        "paginas_ocr": int(OCR_USAGE_ACUM["llamadas"]),
                        "origen": f"contenedor_{tipo}",
                        "contrato_id": cid,
                    },
                }).execute()
            except Exception as e:
                print(f"  [warn] log_uso_ia OCR contenedor: {e}", flush=True)
        try:
            rechunk_embed_pdf(supa, cid)
        except Exception as e:
            print(f"  [warn] rechunk/embed id={cid}: {e}", flush=True)
    return f"ok_{tipo}({chars_utiles(texto)})"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = todos")
    ap.add_argument("--ids", default="", help="Ids fijos separados por coma")
    ap.add_argument("--dry-run", action="store_true", help="No escribe en BD")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    supa = crear_cliente()
    ids = parse_ids(args.ids)

    if ids:
        filas = []
        for i in range(0, len(ids), 80):
            lote = ids[i:i + 80]
            res = (
                supa.table("contratos")
                .select(
                    "id,nro_contratacion,descripcion_contrato,entidad,"
                    "fecha_publica,pdf_descargado,req_url,tdr_texto,"
                    "clasificacion_contrato(categoria_it,relevancia_ia)"
                )
                .in_("id", lote)
                .execute()
            )
            for r in res.data or []:
                _aplanar_cl(r)
                filas.append(r)
    else:
        limit = args.limit if args.limit > 0 else 10**9
        filas = vigentes_ti_sin_tdr(supa, limit)

    print("=" * 60, flush=True)
    print("Fase 3.5 — Contenedores no-PDF (vigentes TI/IA)", flush=True)
    print(
        f"  dry-run={args.dry_run}  ids={ids or '-'}  cola={len(filas)}  "
        f"GEMINI_API_KEY set={bool(GEMINI_API_KEY)}",
        flush=True,
    )
    print("=" * 60, flush=True)

    if not filas:
        print("Nada que hacer (sin vigentes TI/IA sin TDR).", flush=True)
        return

    estados: Counter[str] = Counter()
    ok = 0
    t0 = time.time()
    http = SeaceHttp(headed=args.headed)
    try:
        for i, c in enumerate(filas, 1):
            cid = int(c["id"])
            desc = (c.get("descripcion_contrato") or "")[:50]
            try:
                estado = procesar_contenedor(http, supa, c, args.dry_run)
            except Exception as e:
                estado = f"error({str(e)[:40]})"
            estados[estado] += 1
            if estado.startswith("ok_"):
                ok += 1
            print(f"  [{i}/{len(filas)}] id={cid} {estado}  {desc}", flush=True)
            time.sleep(DELAY_S)
    finally:
        http.close()

    elapsed = time.time() - t0
    print(f"\n{'='*60}", flush=True)
    print(f"Contenedores listos en {elapsed:.0f}s  ok={ok}", flush=True)
    for k, v in sorted(estados.items(), key=lambda kv: -kv[1]):
        print(f"  {k}: {v}", flush=True)
    print("=" * 60, flush=True)
    registrar_run(
        supa,
        PASO_CONTENEDORES,
        {
            "ok": ok,
            "cola": len(filas),
            "elapsed_s": round(elapsed, 1),
            "estados": dict(estados),
            "dry_run": args.dry_run,
            "ids": ids or None,
        },
    )


if __name__ == "__main__":
    main()
