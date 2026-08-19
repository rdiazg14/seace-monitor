#!/usr/bin/env python3
"""
Descubrimiento del endpoint real de descarga del requerimiento (PDF/TDR).

Solo lectura: abre la SPA, entra a la ficha, clic en "Descargar requerimiento"
y captura el trafico. No toca BD, cron ni descargar_requerimiento.py.

Uso:
  uv run python descubrir_endpoint_pdf.py
  uv run python descubrir_endpoint_pdf.py --id 87164 --id 87001
  uv run python descubrir_endpoint_pdf.py --headed --id 87164
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlparse, urlunparse

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

SPA_URL = "https://prod6.seace.gob.pe/buscador-publico/contrataciones"
SPA_BASE = "https://prod6.seace.gob.pe"
IDS_DEFAULT = (87164, 87001)

SESSION_HEADER_KEYS = {
    "cookie", "set-cookie", "authorization", "proxy-authorization",
    "x-csrf-token", "x-xsrf-token", "x-auth-token", "x-access-token",
    "csrf-token", "x-api-key", "api-key", "apikey",
}

KEYWORDS_URL = (
    "requerimiento", "documento", "descarg", "archivo", "pdf", "tdr",
    "anexo", "adjunto",
)

BTN_RE = re.compile(r"descargar\s+requerimiento", re.I)


def es_header_sesion(nombre: str) -> bool:
    lk = (nombre or "").lower()
    if lk in SESSION_HEADER_KEYS:
        return True
    if "cookie" in lk or "token" in lk or "auth" in lk:
        return True
    return False


def headers_publicos(h) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        items = h.items() if hasattr(h, "items") else []
    except Exception:
        items = []
    for k, v in items:
        if es_header_sesion(str(k)):
            continue
        out[str(k)] = str(v)[:300]
    return out


def redactar_body(raw: str | None, cid: int) -> str | None:
    if not raw:
        return None
    texto = raw[:4000]
    try:
        obj = json.loads(texto)
    except Exception:
        return _redactar_texto(texto)
    return json.dumps(_redactar_obj(obj), ensure_ascii=False)[:2000]


def _redactar_obj(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in SESSION_HEADER_KEYS or "cookie" in lk or "token" in lk or "auth" in lk:
                out[k] = "<omitido>"
                continue
            out[k] = _redactar_obj(v)
        return out
    if isinstance(obj, list):
        return [_redactar_obj(x) for x in obj[:30]]
    if isinstance(obj, str) and len(obj) > 400:
        return obj[:400] + "..."
    return obj


def _redactar_texto(texto: str) -> str:
    t = re.sub(r"(?i)(cookie|authorization|token|bearer)=[^&\s]+", r"\1=<omitido>", texto)
    return t[:1500]


def query_params(url: str) -> list[tuple[str, str]]:
    return parse_qsl(urlparse(url).query, keep_blank_values=True)


def plantilla_url(url: str, cid: int) -> str:
    p = urlparse(url)
    path = re.sub(rf"(^|/)({cid})(/|$)", r"\1{id}\3", p.path)
    pares = []
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        pares.append((k, "{id}" if v == str(cid) else v))
    query = "&".join(f"{k}={v}" for k, v in pares)
    return urlunparse((p.scheme, p.netloc, path, p.params, query, ""))


def score_evento(ev: dict, cid: int, t_click: float | None) -> int:
    url = (ev.get("url") or "").lower()
    ct = (ev.get("content_type") or "").lower()
    cd = (ev.get("content_disposition") or "").lower()
    score = 0
    after = t_click is not None and ev.get("t", 0) >= t_click
    if after:
        score += 25
    if "application/pdf" in ct:
        score += 100
    if ".pdf" in cd or "application/pdf" in cd:
        score += 90
    if ev.get("kind") == "download":
        score += 80
    if ev.get("magic_pdf"):
        score += 100
    for kw in KEYWORDS_URL:
        if kw in url:
            score += 35
    if ev.get("resource_type") in ("xhr", "fetch"):
        score += 10
    if str(cid) in (ev.get("url") or ""):
        score += 8
    # ruido conocido
    if any(x in url for x in (
        "listar-completo", "googleapis", "gstatic", "fonts.", ".js", ".css",
        "assets/", "sockjs", "hot-update",
    )):
        score -= 40
    if ev.get("resource_type") in ("stylesheet", "script", "image", "font"):
        score -= 50
    return score


def escanear_bundles(page) -> None:
    """Busca en JS de la SPA rutas/handlers de descarga (sin inventar)."""
    srcs = page.evaluate(
        "() => [...document.querySelectorAll('script[src]')].map(s => s.src).filter(Boolean)"
    ) or []
    print(f"\n  bundles JS: {len(srcs)}", flush=True)
    pats = (
        "requerimiento", "descargar", "download", "application/pdf",
        "archivo", "documento", "tdr", "content-disposition",
    )
    url_re = re.compile(
        r"""['"`](/[^'"`\s]{0,180}?(?:requerimiento|descarg|archivo|documento|pdf)[^'"`\s]{0,120})['"`]""",
        re.I,
    )
    ident_re = re.compile(
        r"[A-Za-z_][A-Za-z0-9_]{0,40}(?:[Rr]equerimiento|[Dd]escarg(?:ar|a)|[Aa]rchivo|[Dd]ocumento)",
    )
    hits = 0
    for src in srcs:
        if "seace.gob.pe" not in src and "osce.gob.pe" not in src:
            continue
        try:
            r = page.request.get(src, timeout=60_000)
            text = r.text()
        except Exception as e:
            print(f"    bundle fail {src[-80:]}: {e}", flush=True)
            continue
        low = text.lower()
        if not any(p in low for p in pats):
            continue
        print(f"    HIT bundle {src.split('/')[-1]} n={len(text)}", flush=True)
        for m in url_re.finditer(text):
            hits += 1
            print(f"      url-in-js: {m.group(1)}", flush=True)
        seen = set()
        for m in ident_re.finditer(text):
            s = m.group(0)
            if s in seen:
                continue
            seen.add(s)
            if len(seen) <= 40:
                print(f"      ident: {s}", flush=True)
        # contextos cortos
        for p in ("requerimiento", "descargarRequerimiento", "descargar-requerimiento",
                  "descargar_requerimiento", "/pdf", "application/pdf"):
            idx = low.find(p.lower())
            if idx < 0:
                continue
            frag = text[max(0, idx - 80): idx + 120].replace("\n", " ")
            print(f"      ctx[{p}]: ...{frag}...", flush=True)
    print(f"  bundle url-hits={hits}", flush=True)


def inspeccionar_listado(page) -> None:
    print("\n  --- listado SPA ---", flush=True)
    try:
        info = page.evaluate(
            """
            () => {
              const vis = (el) => (el.innerText || el.textContent || '')
                .trim().replace(/\\s+/g, ' ').slice(0, 180);
              const clickables = [...document.querySelectorAll(
                'button, a, p-button, [role="button"], mat-icon, i, span[class*="icon"]'
              )].map(el => vis(el)).filter(t => t);
              const interesting = clickables.filter(t =>
                /descarg|requer|pdf|archivo|documento|adjunt|tdr|download/i.test(t)
              );
              const body = (document.body && document.body.innerText || '')
                .replace(/\\s+/g, ' ').slice(0, 1800);
              return {
                title: document.title,
                n_click: clickables.length,
                interesting: interesting.slice(0, 30),
                sample: clickables.slice(0, 25),
                body,
              };
            }
            """
        )
        print(f"  title={info.get('title')!r} n_clickables={info.get('n_click')}", flush=True)
        print(f"  interesting={info.get('interesting')}", flush=True)
        print(f"  sample={info.get('sample')}", flush=True)
        print(f"  body: {(info.get('body') or '')[:1500]}", flush=True)
    except Exception as e:
        print(f"  listado error: {e}", flush=True)
    shot = Path(tempfile.gettempdir()) / "seace-listado.png"
    try:
        page.screenshot(path=str(shot), full_page=False)
        print(f"  screenshot={shot}", flush=True)
    except Exception:
        pass


def dump_pagina(page, cid: int) -> None:
    print(f"  url_final={page.url}", flush=True)
    print(f"  title={page.title()!r}", flush=True)
    frames = [{"name": fr.name, "url": fr.url} for fr in page.frames]
    print(f"  frames={frames}", flush=True)
    try:
        info = page.evaluate(
            """
            () => {
              const btns = [...document.querySelectorAll(
                'button, a, p-button, [role="button"], input[type="button"], input[type="submit"]'
              )].map(el => ({
                tag: el.tagName,
                text: (el.innerText || el.textContent || el.value || '')
                  .trim().replace(/\\s+/g, ' ').slice(0, 160),
                href: el.getAttribute('href') || null,
              }));
              const body = (document.body && document.body.innerText || '')
                .replace(/\\s+/g, ' ').slice(0, 2500);
              return {
                n_buttons: btns.length,
                buttons: btns.slice(0, 40),
                body,
                n_p_button: document.querySelectorAll('p-button').length,
              };
            }
            """
        )
        print(f"  n_buttons={info.get('n_buttons')} p-button={info.get('n_p_button')}", flush=True)
        for b in info.get("buttons") or []:
            if b.get("text") or b.get("href"):
                print(f"    [{b.get('tag')}] {b.get('text')!r} href={b.get('href')!r}", flush=True)
        print(f"  body_text: {(info.get('body') or '')[:2000]}", flush=True)
    except Exception as e:
        print(f"  dump evaluate error: {e}", flush=True)
    shot = Path(tempfile.gettempdir()) / f"seace-detail-{cid}.png"
    try:
        page.screenshot(path=str(shot), full_page=True)
        print(f"  screenshot={shot}", flush=True)
    except Exception as e:
        print(f"  screenshot error: {e}", flush=True)


def inspeccionar_dom(page) -> list[dict]:
    return page.evaluate(
        """
        () => {
          const els = [...document.querySelectorAll(
            'button, a, p-button, [role="button"], span, div, input'
          )];
          const hit = els.filter(el => {
            const t = ((el.innerText || el.textContent || '') + ' ' +
                       (el.getAttribute('aria-label') || '') + ' ' +
                       (el.getAttribute('title') || '') + ' ' +
                       (el.getAttribute('label') || '')).toLowerCase();
            return t.includes('requerimiento') || t.includes('descargar');
          }).slice(0, 25);
          const pack = (el) => {
            const attrs = {};
            for (const a of el.attributes || []) {
              const n = a.name.toLowerCase();
              if (n.includes('cookie') || n.includes('token') || n === 'authorization') continue;
              attrs[a.name] = (a.value || '').slice(0, 300);
            }
            return {
              tag: el.tagName,
              text: (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 120),
              href: el.getAttribute('href') || el.href || null,
              onclick: el.getAttribute('onclick'),
              className: String(el.className || '').slice(0, 200),
              id: el.id || null,
              type: el.getAttribute('type'),
              ng: el.getAttribute('ng-click') || el.getAttribute('(click)'),
              attrs,
              outer: (el.outerHTML || '').slice(0, 600),
            };
          };
          return hit.map(pack);
        }
        """
    )


def localizar_boton_listado(page, cid: int) -> dict:
    """El boton 'Descargar requerimiento' esta en la tarjeta del listado,
    junto al <a href="/buscador-publico/contrataciones/{id}">."""
    return page.evaluate(
        """
        (cid) => {
          const hrefHit = (h) => (h || '').includes('/contrataciones/' + cid);
          const links = [...document.querySelectorAll('a')].filter(a =>
            hrefHit(a.getAttribute('href')) || hrefHit(a.href));
          if (!links.length) return {ok: false, reason: 'sin enlace detalle en listado'};
          const a = links[0];
          let root = a.parentElement;
          while (root && root !== document.body) {
            if (/descargar\\s+requerimiento/i.test(root.innerText || '')) break;
            root = root.parentElement;
          }
          if (!root || root === document.body)
            return {ok: false, reason: 'sin contenedor con boton'};
          const btn = [...root.querySelectorAll('button, a, [role="button"]')]
            .find(el => /descargar\\s+requerimiento/i.test(el.innerText || ''));
          if (!btn) return {ok: false, reason: 'sin boton en tarjeta'};
          [...document.querySelectorAll('[data-seace-dl]')].forEach(el =>
            el.removeAttribute('data-seace-dl'));
          btn.setAttribute('data-seace-dl', '1');
          const attrs = {};
          for (const at of btn.attributes) {
            const n = at.name.toLowerCase();
            if (n.includes('cookie') || n.includes('token') || n === 'authorization') continue;
            attrs[at.name] = (at.value || '').slice(0, 300);
          }
          return {
            ok: true,
            tag: btn.tagName,
            text: (btn.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 160),
            href: btn.getAttribute('href') || null,
            className: String(btn.className || '').slice(0, 240),
            attrs,
            outer: (btn.outerHTML || '').slice(0, 900),
            detalle_href: a.getAttribute('href') || a.href || null,
          };
        }
        """,
        cid,
    )


def localizar_boton(page):
    candidatos = [
        page.get_by_role("button", name=BTN_RE),
        page.get_by_text(BTN_RE),
        page.locator("button").filter(has_text=BTN_RE),
        page.locator("p-button").filter(has_text=BTN_RE),
        page.locator("a").filter(has_text=BTN_RE),
        page.locator("[aria-label]").filter(has_text=BTN_RE),
    ]
    for loc in candidatos:
        try:
            if loc.count() > 0:
                first = loc.first
                if first.is_visible(timeout=2_000):
                    return first
        except Exception:
            continue
    return None


def magic_pdf_de_response(response) -> bool:
    try:
        ct = (response.headers.get("content-type") or "").lower()
        if "json" in ct or "text/html" in ct or "javascript" in ct:
            return False
        if "pdf" in ct or "octet-stream" in ct or "application/download" in ct:
            body = response.body()
            return bool(body) and body.lstrip().startswith(b"%PDF")
    except Exception:
        return False
    return False


def capturar_json_claves(response) -> dict | None:
    try:
        ct = (response.headers.get("content-type") or "").lower()
        if "json" not in ct:
            return None
        body = response.json()
    except Exception:
        return None
    urls: list[str] = []

    def walk(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj[:20]):
                walk(v, f"{path}[{i}]")
        elif isinstance(obj, str):
            lo = obj.lower()
            if any(k in lo for k in KEYWORDS_URL) or obj.startswith("http"):
                urls.append(f"{path}={obj[:250]}")

    walk(body)
    keys = list(body.keys()) if isinstance(body, dict) else ["<list>"]
    return {"keys": keys[:40], "pistas": urls[:15]}


def probar_get(page, url: str) -> dict:
    if not url or url.startswith("blob:"):
        return {"ok": False, "motivo": "url vacia o blob:"}
    try:
        r = page.request.get(url, timeout=60_000)
        body = r.body() or b""
        ct = (r.headers.get("content-type") or "")
        cd = (r.headers.get("content-disposition") or "")
        es = body.lstrip().startswith(b"%PDF")
        return {
            "ok": es and r.status == 200,
            "status": r.status,
            "bytes": len(body),
            "content_type": ct[:120],
            "content_disposition": cd[:200],
            "magic_pdf": es,
        }
    except Exception as e:
        return {"ok": False, "motivo": str(e)[:300]}


def imprimir_evento(ev: dict, cid: int) -> None:
    print(f"    score={ev.get('score', 0)}  {ev.get('kind')}  "
          f"{ev.get('method')} {ev.get('resource_type')}  "
          f"status={ev.get('status')}  {ev.get('url')}", flush=True)
    print(f"      content-type: {ev.get('content_type')}", flush=True)
    if ev.get("content_disposition"):
        print(f"      content-disposition: {ev.get('content_disposition')}", flush=True)
    qp = query_params(ev.get("url") or "")
    if qp:
        print(f"      query: {qp}", flush=True)
    pub = ev.get("headers_publicos") or {}
    if pub:
        print(f"      headers (sin sesion): {json.dumps(pub, ensure_ascii=False)}", flush=True)
    if ev.get("post_data"):
        print(f"      POST body: {ev['post_data']}", flush=True)
    if ev.get("json_pistas"):
        print(f"      json: {json.dumps(ev['json_pistas'], ensure_ascii=False)[:800]}", flush=True)
    if ev.get("suggested_filename"):
        print(f"      download filename: {ev['suggested_filename']}", flush=True)
    print(f"      plantilla: {plantilla_url(ev.get('url') or '', cid)}", flush=True)


def descubrir_uno(page, context, cid: int) -> dict:
    eventos: list[dict] = []
    t0 = time.time()
    t_click: list[float | None] = [None]
    descargas: list[dict] = []

    def on_request(req):
        try:
            eventos.append({
                "kind": "request",
                "t": time.time() - t0,
                "url": req.url,
                "method": req.method,
                "resource_type": req.resource_type,
                "post_data": redactar_body(req.post_data, cid),
                "headers_publicos": headers_publicos(req.headers),
            })
        except Exception:
            pass

    def on_response(resp):
        try:
            h = resp.headers
            ev = {
                "kind": "response",
                "t": time.time() - t0,
                "url": resp.url,
                "method": resp.request.method,
                "resource_type": resp.request.resource_type,
                "status": resp.status,
                "content_type": h.get("content-type") or h.get("Content-Type") or "",
                "content_disposition": h.get("content-disposition") or h.get("Content-Disposition") or "",
                "post_data": redactar_body(resp.request.post_data, cid),
                "headers_publicos": headers_publicos(h),
                "magic_pdf": magic_pdf_de_response(resp),
            }
            jp = capturar_json_claves(resp)
            if jp:
                ev["json_pistas"] = jp
            eventos.append(ev)
        except Exception:
            pass

    def on_download(dl):
        try:
            descargas.append({
                "kind": "download",
                "t": time.time() - t0,
                "url": dl.url,
                "suggested_filename": dl.suggested_filename,
                "method": "GET",
                "resource_type": "download",
            })
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_response)
    context.on("download", on_download)

    detail = f"{SPA_BASE}/buscador-publico/contrataciones/{cid}"
    print(f"\n--- contrato {cid} ---", flush=True)
    print("  el boton vive en el LISTADO, no en la ficha de detalle", flush=True)
    print(f"  goto listado {SPA_URL}", flush=True)
    page.goto(SPA_URL, wait_until="networkidle", timeout=90_000)
    page.wait_for_timeout(2_000)

    info_btn = localizar_boton_listado(page, cid)
    print(f"  boton listado: {json.dumps(info_btn, ensure_ascii=False)[:1200]}", flush=True)
    dom = [info_btn] if info_btn else []

    btn = None
    if info_btn and info_btn.get("ok"):
        btn = page.locator('[data-seace-dl="1"]').first
    if btn is None:
        btn = localizar_boton(page)

    click_ok = False
    download_info = None
    if btn is None or (info_btn and not info_btn.get("ok")):
        print("  BOTON no localizado en listado para este id.", flush=True)
    else:
        print("  boton visible, click + expect_download", flush=True)
        t_click[0] = time.time() - t0
        try:
            with page.expect_download(timeout=25_000) as di:
                btn.click(timeout=10_000)
            dl = di.value
            download_info = {
                "kind": "download",
                "t": time.time() - t0,
                "url": dl.url,
                "suggested_filename": dl.suggested_filename,
                "method": "GET",
                "resource_type": "download",
            }
            descargas.append(download_info)
            click_ok = True
            print(
                f"  download nativo: file={dl.suggested_filename} url={dl.url}",
                flush=True,
            )
        except PlaywrightTimeout:
            click_ok = True
            print("  click hecho; no hubo download nativo (timeout 25s). Se mira XHR.", flush=True)
        except Exception as e:
            print(f"  click/download error: {e}", flush=True)
            try:
                btn.click(timeout=5_000)
                click_ok = True
                page.wait_for_timeout(8_000)
            except Exception as e2:
                print(f"  click fallback error: {e2}", flush=True)

    page.wait_for_timeout(4_000)

    try:
        page.remove_listener("request", on_request)
        page.remove_listener("response", on_response)
        context.remove_listener("download", on_download)
    except Exception:
        pass

    t_c = t_click[0]
    post_click = [e for e in eventos if t_c is not None and e.get("t", 0) >= t_c]
    todos = eventos + descargas
    for ev in todos:
        ev["score"] = score_evento(ev, cid, t_c)

    ranked = sorted(
        [e for e in todos if e.get("kind") in ("response", "download")],
        key=lambda e: e.get("score", 0),
        reverse=True,
    )
    # unique by url+method
    seen: set[str] = set()
    uniq = []
    for e in ranked:
        k = f"{e.get('method')}|{e.get('url')}"
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)

    print(f"  trafico total={len(eventos)}  post-click={len(post_click)}  "
          f"downloads={len(descargas)}  click_ok={click_ok}", flush=True)
    print("  POST-CLICK (todas):", flush=True)
    if not post_click and not descargas:
        print("    (nada)", flush=True)
    for e in post_click + descargas:
        if e.get("kind") == "request" and e.get("resource_type") in (
            "stylesheet", "script", "image", "font",
        ):
            continue
        print(
            f"    t=+{e.get('t', 0):.1f}s {e.get('kind')} {e.get('method')} "
            f"{e.get('resource_type')} {e.get('status')} {e.get('url')}",
            flush=True,
        )

    print("  CANDIDATAS rankeadas:", flush=True)
    top = [e for e in uniq if e.get("score", 0) >= 30][:12]
    if not top:
        print("    (ninguna con score>=30)", flush=True)
    for e in top:
        imprimir_evento(e, cid)

    mejor = top[0] if top else None
    get_test = None
    if mejor and (mejor.get("method") or "GET").upper() == "GET":
        print("  prueba GET con cookies Playwright:", flush=True)
        get_test = probar_get(page, mejor.get("url") or "")
        print(f"    {json.dumps(get_test, ensure_ascii=False)}", flush=True)
    elif mejor:
        print(
            f"  no se prueba GET: metodo={mejor.get('method')} "
            "(descargar_requerimiento.py asume GET)",
            flush=True,
        )

    return {
        "id": cid,
        "detail": detail,
        "click_ok": click_ok,
        "dom": dom,
        "mejor": mejor,
        "top": top,
        "get_test": get_test,
        "download": download_info or (descargas[0] if descargas else None),
        "plantilla": plantilla_url(mejor["url"], cid) if mejor and mejor.get("url") else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, action="append", dest="ids",
                    help="idContrato (repetible). Default: 87164 y 87001")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    ids = args.ids or list(IDS_DEFAULT)

    print("=" * 64, flush=True)
    print("Descubrimiento endpoint PDF/requerimiento SEACE", flush=True)
    print(f"  ids={ids}  headed={args.headed}", flush=True)
    print("  (cookies/Authorization/tokens omitidos del reporte)", flush=True)
    print("=" * 64, flush=True)

    resultados = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context = browser.new_context(
            ignore_https_errors=True,
            accept_downloads=True,
        )
        page = context.new_page()
        print("Sesion SPA...", flush=True)
        page.goto(SPA_URL, wait_until="networkidle", timeout=90_000)
        page.wait_for_timeout(2_000)
        print("Sesion lista.", flush=True)
        inspeccionar_listado(page)
        escanear_bundles(page)

        for cid in ids:
            resultados.append(descubrir_uno(page, context, cid))
            time.sleep(0.8)

        browser.close()

    print("\n" + "=" * 64, flush=True)
    print("REPORTE FINAL", flush=True)
    print("=" * 64, flush=True)

    plantillas = []
    for r in resultados:
        print(f"\n[{r['id']}] click_ok={r['click_ok']}", flush=True)
        m = r.get("mejor")
        if not m:
            print("  sin candidata de red. DOM:", flush=True)
            for d in (r.get("dom") or [])[:5]:
                print(f"    {d.get('tag')} {d.get('text')!r} href={d.get('href')!r}", flush=True)
                print(f"    outer={d.get('outer')}", flush=True)
            continue
        print(f"  URL: {m.get('url')}", flush=True)
        print(f"  metodo: {m.get('method')}", flush=True)
        print(f"  query: {query_params(m.get('url') or '')}", flush=True)
        print(f"  content-type: {m.get('content_type')}", flush=True)
        print(f"  content-disposition: {m.get('content_disposition')}", flush=True)
        if m.get("post_data"):
            print(f"  POST body: {m.get('post_data')}", flush=True)
        print(f"  headers relevantes: {json.dumps(m.get('headers_publicos') or {}, ensure_ascii=False)}", flush=True)
        print(f"  REQ_PDF_URL propuesto: {r.get('plantilla')}", flush=True)
        gt = r.get("get_test") or {}
        print(f"  GET+cookies Playwright basta: {gt.get('ok')}  {gt}", flush=True)
        plantillas.append(r.get("plantilla"))

    unicas = {x for x in plantillas if x}
    print("\nPatron estable entre ids:", flush=True)
    if len(unicas) == 1:
        print(f"  SI. Una sola plantilla:\n  {unicas.pop()}", flush=True)
    elif unicas:
        print("  NO. Plantillas distintas:", flush=True)
        for u in unicas:
            print(f"    {u}", flush=True)
    else:
        print("  no hay plantilla (no se capturo descarga).", flush=True)
    print("=" * 64, flush=True)


if __name__ == "__main__":
    main()
