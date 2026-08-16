#!/usr/bin/env python3
"""
Fase 3 — Descarga y extracción de PDF/TDR (solo vigentes).

Idempotente: SELECT estado='Vigente' AND pdf_descargado=false.

Flujo por contrato (2 GET, capturados del SEACE):
  1) LISTAR_URL  → JSON de anexos
  2) elige application/pdf (prioriza idTipoArchivo=1)
  3) DESCARGAR_URL → binario
  4) PyMuPDF → OCR fallback → tdr_texto → borra temp SIEMPRE

httpx directo. Playwright solo si alguna GET da 401/403.

Uso:
  uv run python descargar_requerimiento.py --dry-run --limit 5
  uv run python descargar_requerimiento.py --limit 20
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

import httpx
import pymupdf
from playwright.sync_api import sync_playwright
from supabase import create_client

from ingesta_completa import registrar_rechazo

_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
SPA_URL = "https://prod6.seace.gob.pe/buscador-publico/contrataciones"
GEMINI_FLASH = "gemini-3.7-flash"

# Plantillas reales capturadas (descubrir_endpoint_pdf.py). Override por env.
LISTAR_URL = os.environ.get(
    "LISTAR_URL",
    "https://prod6.seace.gob.pe/v1/s8uit-services/archivo"
    "/archivos-publico/listar-archivos-contrato/{idContrato}/1",
).strip()
DESCARGAR_URL = os.environ.get(
    "DESCARGAR_URL",
    "https://prod6.seace.gob.pe/v1/s8uit-services/archivo"
    "/archivos-publico/descargar-archivo-contrato/{idContratoArchivo}",
).strip()

PAGE_DB = 1_000
MIN_CHARS_PAGINA = 80
OCR_MAX_PAGINAS = 40
OCR_DPI = 150
DELAY_S = 0.35
TEMP_PREFIX = "seace-tdr-"
PREVIEW_CHARS = 1_500
MOTIVO_SIN_PDF = "sin archivo PDF"


class SinPdf(Exception):
    """Listado vacio o sin application/pdf. No se reintenta."""

    def __init__(self, archivos: list):
        super().__init__(MOTIVO_SIN_PDF)
        self.archivos = archivos or []


class PdfExtractError(Exception):
    """Fallo de parse/OCR despues de listar+descargar. El temp ya se borro."""

    def __init__(self, msg: str, meta: dict):
        super().__init__(msg)
        self.meta = meta


class SeaceHttp:
    """GET con httpx. Playwright solo ante 401/403."""

    def __init__(self, headed: bool = False):
        self.headed = headed
        self._client = httpx.Client(
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
            headers={"Accept": "*/*"},
        )
        self._pw = None
        self._browser = None
        self._page = None

    def get_bytes(self, url: str) -> tuple[int, dict[str, str], bytes]:
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = self._client.get(url)
                if r.status_code in (401, 403):
                    print(
                        f"  [fallback] Playwright por HTTP {r.status_code}",
                        flush=True,
                    )
                    return self._get_pw(url)
                if r.status_code >= 500:
                    last = RuntimeError(f"HTTP {r.status_code}")
                    time.sleep(1.0 * (attempt + 1))
                    continue
                headers = {k.lower(): v for k, v in r.headers.items()}
                return r.status_code, headers, r.content or b""
            except httpx.HTTPError as e:
                last = e
                time.sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"GET fallo: {last}")

    def _get_pw(self, url: str) -> tuple[int, dict[str, str], bytes]:
        page = self._ensure_pw()
        r = page.request.get(url, timeout=60_000)
        headers = {k.lower(): v for k, v in r.headers.items()}
        return r.status, headers, r.body() or b""

    def _ensure_pw(self):
        if self._page is not None:
            return self._page
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=not self.headed)
        self._page = self._browser.new_context(ignore_https_errors=True).new_page()
        self._page.goto(SPA_URL, wait_until="networkidle", timeout=90_000)
        self._page.wait_for_timeout(2_000)
        return self._page

    def close(self) -> None:
        self._client.close()
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._pw is not None:
                self._pw.stop()


def chars_utiles(texto: str) -> int:
    return sum(1 for c in (texto or "") if c.isalnum())


def limpiar_texto(texto: str) -> str:
    t = (texto or "").replace("\x00", " ")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def pendientes_pdf(supa, limit: int) -> list[dict]:
    """Vigentes sin PDF, mas recientes primero."""
    out: list[dict] = []
    offset = 0
    while len(out) < limit:
        take = min(PAGE_DB, limit - len(out))
        res = (
            supa.table("contratos")
            .select(
                "id,nro_contratacion,descripcion_contrato,entidad,"
                "fecha_publica,pdf_descargado"
            )
            .eq("estado", "Vigente")
            .or_("pdf_descargado.eq.false,pdf_descargado.is.null")
            .order("fecha_publica", desc=True, nullsfirst=False)
            .order("id", desc=True)
            .range(offset, offset + take - 1)
            .execute()
        )
        batch = res.data or []
        out.extend(batch)
        if len(batch) < take:
            break
        offset += take
    return out[:limit]


def pdf_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _es_pdf(body: bytes, ctype: str) -> bool:
    head = body[:16].lstrip()
    if head.startswith(b"%PDF"):
        return True
    cl = (ctype or "").lower()
    return "application/pdf" in cl or cl.endswith("/pdf")


def _parece_html(body: bytes) -> bool:
    sample = body[:400].lstrip().lower()
    return (
        sample.startswith(b"<!doctype")
        or sample.startswith(b"<html")
        or b"<html" in sample[:200]
    )


def resumen_archivos(archivos: list) -> list[dict]:
    out = []
    for a in archivos:
        if not isinstance(a, dict):
            continue
        out.append({
            "idContratoArchivo": a.get("idContratoArchivo"),
            "idTipoArchivo": a.get("idTipoArchivo"),
            "nombre": a.get("nombre"),
            "descripcionMime": a.get("descripcionMime"),
        })
    return out


def elegir_pdf(archivos: list) -> dict | None:
    pdfs = [
        a for a in archivos
        if isinstance(a, dict)
        and str(a.get("descripcionMime") or "").lower() == "application/pdf"
    ]
    if not pdfs:
        return None
    tipo1 = [a for a in pdfs if a.get("idTipoArchivo") == 1]
    return (tipo1 or pdfs)[0]


def listar_archivos(http: SeaceHttp, cid: int) -> tuple[str, list]:
    url = LISTAR_URL.format(idContrato=cid, id=cid, id_contrato=cid)
    status, _headers, body = http.get_bytes(url)
    if status != 200:
        raise RuntimeError(f"listar HTTP {status}")
    try:
        data = json.loads(body)
    except Exception as e:
        raise RuntimeError(f"listar JSON invalido: {e}") from e
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                data = v
                break
    if not isinstance(data, list):
        raise RuntimeError(f"listar no es lista ({type(data).__name__})")
    return url, data


def descargar_binario(http: SeaceHttp, url: str, dest: Path) -> None:
    status, headers, body = http.get_bytes(url)
    ctype = headers.get("content-type") or ""
    if status != 200:
        raise RuntimeError(f"descargar HTTP {status} ({ctype[:80]})")
    if not body:
        raise RuntimeError("respuesta vacia al descargar PDF")
    if _parece_html(body) or "json" in ctype.lower() or "text/html" in ctype.lower():
        raise RuntimeError(f"no es PDF (content-type={ctype[:80]} n={len(body)})")
    if not _es_pdf(body, ctype):
        raise RuntimeError(
            f"binario no es PDF (content-type={ctype[:80]} n={len(body)} "
            f"magic={body[:8]!r})"
        )
    dest.write_bytes(body)


def ocr_pagina_gemini(img_bytes: bytes, mime: str = "image/jpeg") -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY ausente; no se puede hacer OCR")
    b64 = base64.b64encode(img_bytes).decode("ascii")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_FLASH}:generateContent"
    )
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {
                    "text": (
                        "Extrae TODO el texto visible de esta pagina de un "
                        "requerimiento/TDR de contratacion publica peruana (SEACE). "
                        "Responde solo el texto, en espanol, sin preambulo ni markdown."
                    ),
                },
                {"inlineData": {"mimeType": mime, "data": b64}},
            ],
        }],
        "generationConfig": {
            "thinkingConfig": {"thinkingLevel": "LOW"},
            "maxOutputTokens": 4096,
            "temperature": 0.1,
        },
    }
    last_err: Exception | None = None
    for wait in (0.0, 2.0, 8.0, 20.0):
        if wait:
            time.sleep(wait)
        try:
            r = httpx.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": GEMINI_API_KEY,
                },
                json=payload,
                timeout=120.0,
            )
            if r.status_code == 429:
                last_err = RuntimeError(f"429 OCR: {r.text[:160]}")
                continue
            r.raise_for_status()
            body = r.json()
            parts = (
                (body.get("candidates") or [{}])[0]
                .get("content", {})
                .get("parts") or []
            )
            textos = [
                p.get("text") or ""
                for p in parts
                if not p.get("thought")
            ]
            return limpiar_texto("\n".join(textos))
        except Exception as e:
            last_err = e
    raise RuntimeError(f"OCR Gemini fallo: {last_err}")


def extraer_paginas(path: Path) -> dict:
    paginas_txt: list[str] = []
    por_pagina: list[dict] = []
    ocr_idx: list[int] = []
    chars_pymupdf_total = 0
    n = 0

    with pymupdf.open(path) as doc:
        n = doc.page_count
        if n == 0:
            raise RuntimeError("PDF sin paginas")
        nativos: list[str] = []
        for i, page in enumerate(doc, 1):
            nativo = (page.get_text("text") or "").strip()
            n_nat = chars_utiles(nativo)
            chars_pymupdf_total += n_nat
            nativos.append(nativo)
            por_pagina.append({
                "pagina": i,
                "chars_pymupdf": n_nat,
                "ocr": n_nat < MIN_CHARS_PAGINA,
                "chars_final": n_nat,
            })

        for i, page in enumerate(doc, 1):
            nativo = nativos[i - 1]
            n_nat = por_pagina[i - 1]["chars_pymupdf"]
            uso_ocr = n_nat < MIN_CHARS_PAGINA
            texto_final = limpiar_texto(nativo)
            if uso_ocr:
                if len(ocr_idx) >= OCR_MAX_PAGINAS:
                    por_pagina[i - 1]["omitido"] = "ocr_max"
                    por_pagina[i - 1]["ocr"] = False
                    paginas_txt.append(texto_final)
                    continue
                pix = page.get_pixmap(dpi=OCR_DPI, alpha=False)
                try:
                    ocr = ocr_pagina_gemini(pix.tobytes("jpeg"), "image/jpeg")
                except Exception as e:
                    pendientes = [p["pagina"] for p in por_pagina if p.get("ocr")]
                    raise PdfExtractError(str(e), {
                        "n_paginas": n,
                        "chars_pymupdf": chars_pymupdf_total,
                        "por_pagina": por_pagina,
                        "ocr_paginas": pendientes,
                        "pdf_es_imagen": True,
                        "tdr_texto": "",
                        "chars_final": chars_pymupdf_total,
                    }) from e
                ocr_idx.append(i)
                texto_final = ocr or texto_final
                por_pagina[i - 1]["chars_final"] = chars_utiles(texto_final)
            paginas_txt.append(texto_final)

    bloques = []
    for i, t in enumerate(paginas_txt, 1):
        if t:
            bloques.append(f"--- pagina {i} ---\n{t}")
    texto = limpiar_texto("\n\n".join(bloques))
    if chars_utiles(texto) == 0:
        raise RuntimeError("texto extraido vacio (PyMuPDF+OCR)")
    return {
        "texto": texto,
        "ocr_paginas": ocr_idx,
        "pdf_es_imagen": bool(ocr_idx),
        "n_paginas": n,
        "chars_pymupdf": chars_pymupdf_total,
        "chars_final": chars_utiles(texto),
        "por_pagina": por_pagina,
    }


def borrar_temp(tmp: Path) -> None:
    try:
        tmp.unlink(missing_ok=True)
    except OSError as e:
        print(f"  [warn] no se pudo borrar {tmp}: {e}", flush=True)


def procesar_contrato(http: SeaceHttp, contrato: dict) -> dict:
    cid = int(contrato["id"])
    _listar_url, archivos = listar_archivos(http, cid)
    elegido = elegir_pdf(archivos)
    if elegido is None:
        raise SinPdf(archivos)

    aid = elegido.get("idContratoArchivo")
    if not aid:
        raise RuntimeError("PDF sin idContratoArchivo")
    dl_url = DESCARGAR_URL.format(
        idContratoArchivo=aid,
        id=aid,
        id_archivo=aid,
    )

    fd, tmp_name = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".pdf")
    os.close(fd)
    tmp = Path(tmp_name)
    meta = {
        "id": cid,
        "url": dl_url,
        "pdf_archivo_id": int(aid),
        "pdf_nombre": elegido.get("nombre"),
        "pdf_mime": elegido.get("descripcionMime"),
        "id_tipo_archivo": elegido.get("idTipoArchivo"),
        "n_archivos": len(archivos),
        "archivos": resumen_archivos(archivos),
        "temp_path": str(tmp),
        "bytes": 0,
        "pdf_hash": "",
        "tdr_texto": "",
        "por_pagina": [],
        "ocr_paginas": [],
        "n_paginas": 0,
        "chars_pymupdf": 0,
        "chars_final": 0,
        "pdf_es_imagen": False,
    }
    try:
        descargar_binario(http, dl_url, tmp)
        meta["pdf_hash"] = pdf_sha256(tmp)
        meta["bytes"] = tmp.stat().st_size if tmp.exists() else 0
        try:
            extra = extraer_paginas(tmp)
        except PdfExtractError as e:
            e.meta = {**meta, **e.meta}
            raise
        meta.update({
            "tdr_texto": extra["texto"],
            "pdf_es_imagen": extra["pdf_es_imagen"],
            "n_paginas": extra["n_paginas"],
            "chars_pymupdf": extra["chars_pymupdf"],
            "chars_final": extra["chars_final"],
            "ocr_paginas": extra["ocr_paginas"],
            "por_pagina": extra["por_pagina"],
        })
        return meta
    except PdfExtractError:
        raise
    except Exception as e:
        raise PdfExtractError(str(e), meta) from e
    finally:
        borrar_temp(tmp)


def _update_contrato(supa, cid: int, payload: dict) -> None:
    extra = {
        "pdf_archivo_id": payload.pop("_pdf_archivo_id", None),
        "pdf_nombre": payload.pop("_pdf_nombre", None),
    }
    full = dict(payload)
    if extra["pdf_archivo_id"] is not None:
        full["pdf_archivo_id"] = extra["pdf_archivo_id"]
    if extra["pdf_nombre"] is not None:
        full["pdf_nombre"] = extra["pdf_nombre"]
    try:
        supa.table("contratos").update(full).eq("id", cid).execute()
    except Exception as e:
        msg = str(e).lower()
        if "pdf_archivo_id" in msg or "pdf_nombre" in msg:
            print(
                "  [warn] faltan columnas pdf_archivo_id/pdf_nombre; "
                "ejecuta pdf_archivo_meta.sql",
                flush=True,
            )
            supa.table("contratos").update(payload).eq("id", cid).execute()
        else:
            raise


def guardar_ok(supa, row: dict) -> None:
    _update_contrato(supa, row["id"], {
        "tdr_texto": row["tdr_texto"] or None,
        "pdf_hash": row["pdf_hash"],
        "pdf_es_imagen": row["pdf_es_imagen"],
        "pdf_descargado": True,
        "pdf_procesado": True,
        "req_url": (row["url"] or "")[:2000],
        "_pdf_archivo_id": row.get("pdf_archivo_id"),
        "_pdf_nombre": (row.get("pdf_nombre") or "")[:500] or None,
    })


def guardar_sin_pdf(supa, cid: int) -> None:
    """No reintentar: no hay anexo PDF. tdr_texto queda null."""
    _update_contrato(supa, cid, {
        "tdr_texto": None,
        "pdf_hash": None,
        "pdf_es_imagen": None,
        "pdf_descargado": True,
        "pdf_procesado": True,
        "req_url": "sin_pdf",
        "_pdf_archivo_id": None,
        "_pdf_nombre": None,
    })


def payload_rechazo(contrato: dict, motivo: str, extra: dict | None = None) -> dict:
    out = {
        "idContrato": int(contrato["id"]),
        "nroContratacion": contrato.get("nro_contratacion"),
        "desContratacion": contrato.get("descripcion_contrato"),
        "motivo": motivo,
    }
    if extra:
        out.update(extra)
    return out


def imprimir_resultado(i: int, total: int, contrato: dict, row: dict,
                       estado: str = "OK") -> None:
    cid = row["id"]
    desc = (contrato.get("descripcion_contrato") or "")[:50]
    digest = row.get("pdf_hash") or ""
    print(
        f"  [{i}/{total}] id={cid} {estado} {desc}\n"
        f"      listados={row.get('n_archivos')}  "
        f"elegido={row.get('pdf_nombre')!r}  "
        f"mime={row.get('pdf_mime')}  "
        f"idTipoArchivo={row.get('id_tipo_archivo')}  "
        f"idContratoArchivo={row.get('pdf_archivo_id')}",
        flush=True,
    )
    print(
        f"      paginas={row.get('n_paginas')} bytes={row.get('bytes')} "
        f"hash={(digest[:12] + '...') if digest else '-'} "
        f"pymupdf_chars={row.get('chars_pymupdf')} "
        f"final_chars={row.get('chars_final')} "
        f"ocr_paginas={row.get('ocr_paginas') or '-'} "
        f"pdf_es_imagen={row.get('pdf_es_imagen')} "
        f"temp={('AUN EXISTE' if Path(row.get('temp_path') or '').exists() else 'borrado')}",
        flush=True,
    )
    for p in row["por_pagina"]:
        marca = "OCR" if p.get("ocr") else "pymupdf"
        extra = f" omitido={p['omitido']}" if p.get("omitido") else ""
        print(
            f"      p{p['pagina']}: {marca}  "
            f"pymupdf={p['chars_pymupdf']} final={p['chars_final']}{extra}",
            flush=True,
        )
    preview = (row["tdr_texto"] or "")[:PREVIEW_CHARS].replace("\n", " | ")
    mas = "..." if len(row["tdr_texto"] or "") > PREVIEW_CHARS else ""
    print(
        f"      texto ({len(row['tdr_texto'] or '')} chars): {preview}{mas}",
        flush=True,
    )


def imprimir_sin_pdf(i: int, total: int, contrato: dict, archivos: list) -> None:
    cid = int(contrato["id"])
    desc = (contrato.get("descripcion_contrato") or "")[:50]
    print(
        f"  [{i}/{total}] id={cid} SIN_PDF {desc}  listados={len(archivos)}",
        flush=True,
    )
    for a in resumen_archivos(archivos):
        print(
            f"      archivo={a.get('nombre')!r} mime={a.get('descripcionMime')} "
            f"tipo={a.get('idTipoArchivo')} id={a.get('idContratoArchivo')}",
            flush=True,
        )
    if not archivos:
        print("      (listado vacio)", flush=True)


def escribir_resumen(stats: dict) -> None:
    """Log de corrida: OCR_PAGINAS_TOTAL para vigilar el free tier de Flash."""
    from datetime import datetime, timezone

    lines = [
        f"ts={datetime.now(timezone.utc).isoformat()}",
        *[f"{k}={v}" for k, v in stats.items()],
    ]
    texto = "\n".join(lines) + "\n"
    log = Path(__file__).parent / "data" / "ultima_pdf.txt"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(texto, encoding="utf-8")
    except OSError as e:
        print(f"  [warn] no se pudo escribir {log}: {e}", flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        md = (
            "### PDF/TDR\n\n"
            f"- ok: **{stats.get('ok')}** "
            f"(nativo={stats.get('ok_nativo')}, "
            f"con OCR={stats.get('ocr_contratos')})\n"
            f"- **OCR_PAGINAS_TOTAL={stats.get('ocr_paginas_total')}** "
            "(comparte 1,500 req/día de Flash con el chat)\n"
            f"- sin_pdf: {stats.get('sin_pdf')}\n"
            f"- errores: {stats.get('err')}\n"
            f"- dry-run: {stats.get('dry_run')}  "
            f"limit: {stats.get('limit')}  "
            f"t={stats.get('elapsed_s')}s\n"
        )
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(md)
        except OSError:
            pass
    print(texto, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Procesa y muestra; no escribe contratos ni rechazos",
    )
    ap.add_argument("--headed", action="store_true",
                    help="Playwright headed (solo si hay fallback 401/403)")
    args = ap.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SystemExit("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY no encontrados")

    supa = create_client(SUPABASE_URL, SUPABASE_KEY)
    filas = pendientes_pdf(supa, args.limit)

    print("=" * 60, flush=True)
    print("Fase 3 — PDF/TDR (listar + descargar, httpx)", flush=True)
    print(
        f"  dry-run={args.dry_run}  limit={args.limit}  pendientes={len(filas)}",
        flush=True,
    )
    print(f"  LISTAR_URL={LISTAR_URL}", flush=True)
    print(f"  DESCARGAR_URL={DESCARGAR_URL}", flush=True)
    print(f"  GEMINI_API_KEY set={bool(GEMINI_API_KEY)}  (OCR fallback)", flush=True)
    print("=" * 60, flush=True)

    if not filas:
        print("Nada que hacer (todos los vigentes ya tienen pdf_descargado).", flush=True)
        return

    ok = 0
    ok_nativo = 0
    ocr_contratos = 0
    ocr_paginas_total = 0
    sin_pdf = 0
    err = 0
    t0 = time.time()
    leftovers_antes = {
        p.name for p in Path(tempfile.gettempdir()).glob(f"{TEMP_PREFIX}*")
    }

    http = SeaceHttp(headed=args.headed)
    try:
        for i, c in enumerate(filas, 1):
            cid = int(c["id"])
            desc = (c.get("descripcion_contrato") or "")[:50]
            try:
                row = procesar_contrato(http, c)
                imprimir_resultado(i, len(filas), c, row)
                n_ocr = len(row.get("ocr_paginas") or [])
                if n_ocr:
                    ocr_contratos += 1
                    ocr_paginas_total += n_ocr
                    print(f"      OCR_PAGINAS={n_ocr} (acum={ocr_paginas_total})", flush=True)
                else:
                    ok_nativo += 1
                if not args.dry_run:
                    guardar_ok(supa, row)
                ok += 1
            except SinPdf as e:
                sin_pdf += 1
                imprimir_sin_pdf(i, len(filas), c, e.archivos)
                if not args.dry_run:
                    guardar_sin_pdf(supa, cid)
                    registrar_rechazo(
                        supa,
                        payload_rechazo(
                            c,
                            MOTIVO_SIN_PDF,
                            {"archivos": resumen_archivos(e.archivos)},
                        ),
                        MOTIVO_SIN_PDF,
                        origen="pdf",
                    )
            except PdfExtractError as e:
                err += 1
                if e.meta.get("n_archivos") is not None:
                    imprimir_resultado(i, len(filas), c, e.meta, estado=f"FAIL ({e})")
                else:
                    print(
                        f"  [{i}/{len(filas)}] id={cid} FAIL {desc}  {e}",
                        flush=True,
                    )
                if not args.dry_run:
                    registrar_rechazo(
                        supa,
                        payload_rechazo(c, str(e)[:500], {
                            "archivos": e.meta.get("archivos"),
                            "pdf_nombre": e.meta.get("pdf_nombre"),
                        }),
                        str(e),
                        origen="pdf",
                    )
            except Exception as e:
                err += 1
                print(f"  [{i}/{len(filas)}] id={cid} FAIL {desc}  {e}", flush=True)
                if not args.dry_run:
                    registrar_rechazo(
                        supa,
                        payload_rechazo(c, str(e)[:500]),
                        str(e),
                        origen="pdf",
                    )
            time.sleep(DELAY_S)
    finally:
        http.close()

    leftovers = [
        p.name
        for p in Path(tempfile.gettempdir()).glob(f"{TEMP_PREFIX}*")
        if p.name not in leftovers_antes
    ]
    elapsed = time.time() - t0
    print(f"\n{'='*60}", flush=True)
    print(
        f"Listo en {elapsed:.0f}s  ok={ok} nativo={ok_nativo} "
        f"ocr_contratos={ocr_contratos} ocr_paginas={ocr_paginas_total} "
        f"sin_pdf={sin_pdf} err={err} dry-run={args.dry_run}",
        flush=True,
    )
    print(f"OCR_CONTRATOS={ocr_contratos}", flush=True)
    print(f"OCR_PAGINAS_TOTAL={ocr_paginas_total}", flush=True)
    print(
        f"Temps {TEMP_PREFIX}* residuales de esta corrida: "
        f"{leftovers if leftovers else 'ninguno (borrados)'}",
        flush=True,
    )
    print("=" * 60, flush=True)
    escribir_resumen({
        "ok": ok,
        "ok_nativo": ok_nativo,
        "ocr_contratos": ocr_contratos,
        "ocr_paginas_total": ocr_paginas_total,
        "sin_pdf": sin_pdf,
        "err": err,
        "limit": args.limit,
        "dry_run": args.dry_run,
        "elapsed_s": int(elapsed),
        "temps_residuales": ",".join(leftovers),
    })


if __name__ == "__main__":
    main()
