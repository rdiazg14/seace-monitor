#!/usr/bin/env python3
"""
Fase 1.1 — Descubrir la API de detalle del SEACE.
Navega 5 contratos vigentes e intercepta todas las llamadas JSON.
"""
import json
import os
import time
import re
from supabase import create_client
from playwright.sync_api import sync_playwright

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://wusywwhcyqngnpvpzxyr.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SPA_BASE = "https://prod6.seace.gob.pe"

# ─── 1. Obtener 5 contratos vigentes de Supabase ────────────────────
supa = create_client(SUPABASE_URL, SUPABASE_KEY)
res = (supa.table("contratos")
       .select("id, nro_contratacion, descripcion_contrato")
       .eq("estado", "Vigente")
       .order("id", desc=True)
       .limit(5)
       .execute())
vigentes = res.data
print(f"Contratos a probar: {[v['id'] for v in vigentes]}")

all_results = {}
pdf_urls_encontradas = []

# ─── 2. Navegar cada contrato con Playwright ─────────────────────────
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    for contrato in vigentes:
        cid = contrato["id"]
        desc = contrato.get("descripcion_contrato", "")[:70]
        print(f"\n{'='*65}")
        print(f"[{cid}] {desc}")

        captured = []

        ctx = browser.new_context(ignore_https_errors=True)
        page = ctx.new_page()

        # Closure correcta para capturar por referencia al mismo `captured`
        def _make_handler(cap_list):
            def handler(response):
                url = response.url
                ct = response.headers.get("content-type", "")
                if "json" in ct and "seace.gob.pe" in url:
                    try:
                        body = response.json()
                        cap_list.append({
                            "url": url,
                            "method": response.request.method,
                            "status": response.status,
                            "body": body,
                        })
                        print(f"  → {response.request.method} {url}")
                    except Exception:
                        pass
            return handler

        page.on("response", _make_handler(captured))

        detail_url = f"{SPA_BASE}/buscador-publico/contrataciones/{cid}"
        page.goto(detail_url, wait_until="networkidle", timeout=90_000)
        page.wait_for_timeout(3_000)

        # ── Buscar botón de descarga / PDF en el DOM ───────────────
        pdf_url_dom = None
        try:
            # Buscar cualquier anchor o botón con href que contenga pdf/archivo/descarg
            hrefs = page.evaluate("""
                () => {
                    const els = [...document.querySelectorAll('a[href], button[onclick]')];
                    return els
                        .map(el => el.href || el.getAttribute('onclick') || '')
                        .filter(h => h && (
                            h.toLowerCase().includes('pdf') ||
                            h.toLowerCase().includes('descarg') ||
                            h.toLowerCase().includes('archivo') ||
                            h.toLowerCase().includes('requerimiento')
                        ));
                }
            """)
            if hrefs:
                pdf_url_dom = hrefs
                print(f"  → DOM hrefs con PDF/descarga: {hrefs[:3]}")
        except Exception as e:
            print(f"  → Error DOM search: {e}")

        all_results[str(cid)] = {
            "id": cid,
            "nro": contrato.get("nro_contratacion"),
            "descripcion": contrato.get("descripcion_contrato"),
            "detail_url": detail_url,
            "api_calls": captured,
            "pdf_url_dom": pdf_url_dom,
        }

        ctx.close()
        time.sleep(0.5)

    browser.close()

# ─── 3. Guardar resultados completos ────────────────────────────────
os.makedirs("data", exist_ok=True)
with open("data/api_detalle_discovery.json", "w", encoding="utf-8") as f:
    json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)

# ─── 4. Análisis: encontrar endpoint de detalle y PDF ───────────────
print("\n\n" + "="*65)
print("ANÁLISIS DE APIs CAPTURADAS")
print("="*65)

endpoint_detalle = None
endpoint_pdf = None
campos_detalle = {}

for cid, info in all_results.items():
    print(f"\n[{cid}] {info['nro']}")
    for call in info["api_calls"]:
        url = call["url"]
        body = call["body"]
        print(f"  {call['method']} {url}")

        if isinstance(body, dict):
            keys = list(body.keys())
            print(f"    keys: {keys}")

            body_str = json.dumps(body, default=str, ensure_ascii=False).lower()

            # ¿Contiene datos de detalle del contrato?
            if any(k in keys for k in ["idContrato", "nroContratacion", "desContratacion",
                                         "areaUsuaria", "items", "cronograma", "requerimiento"]):
                endpoint_detalle = url
                campos_detalle = keys
                print(f"    *** ENDPOINT DE DETALLE ENCONTRADO ***")

                # Buscar URL de PDF en el cuerpo
                def _buscar_pdf(obj, path=""):
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            _buscar_pdf(v, f"{path}.{k}")
                    elif isinstance(obj, list):
                        for i, v in enumerate(obj):
                            _buscar_pdf(v, f"{path}[{i}]")
                    elif isinstance(obj, str):
                        lo = obj.lower()
                        if (".pdf" in lo or "descarg" in lo or "archivo" in lo or
                                "requerimiento" in lo or "tdr" in lo):
                            print(f"    *** PDF FIELD: {path} = {obj[:200]}")
                            pdf_urls_encontradas.append({"path": path, "value": obj})

                _buscar_pdf(body)

            # ¿Es un endpoint de descarga de PDF?
            if "pdf" in url.lower() or "descarg" in url.lower() or "archivo" in url.lower():
                endpoint_pdf = url
                print(f"    *** ENDPOINT PDF DIRECTO ***")

        elif isinstance(body, list) and body:
            print(f"    list[{len(body)}], first item keys: {list(body[0].keys()) if isinstance(body[0], dict) else '?'}")

    if info.get("pdf_url_dom"):
        print(f"  DOM PDF refs: {info['pdf_url_dom']}")

# ─── 5. Resumen final ─────────────────────────────────────────────
print("\n\n" + "="*65)
print("RESUMEN")
print("="*65)
print(f"Endpoint detalle:  {endpoint_detalle or 'NO ENCONTRADO'}")
print(f"Campos:            {campos_detalle}")
print(f"Endpoint PDF:      {endpoint_pdf or 'NO ENCONTRADO'}")
print(f"PDF fields en body:{len(pdf_urls_encontradas)}")
for pf in pdf_urls_encontradas[:5]:
    print(f"  {pf['path']}: {pf['value'][:150]}")
print(f"\nJSON completo en: data/api_detalle_discovery.json")
