#!/usr/bin/env python3
"""
Scraper del buscador publico de contrataciones menores del SEACE (Peru).

Fuente: https://prod6.seace.gob.pe/buscador-publico/contrataciones
Es una SPA (Angular): el HTML llega vacio y los datos vienen de una API interna
JSON. Este scraper:

  1. Abre la URL con Playwright (chromium), lo que arranca la SPA y establece
     la sesion/cookies necesarias.
  2. Escribe la palabra clave en el buscador y, opcionalmente, marca el filtro
     Objeto; luego da clic en "Buscar". Esto dispara la llamada real a la API,
     que interceptamos con page.on("response", ...) -> demuestra que los
     selectores enganchan y captura registros directamente.
  3. Como via principal y robusta de datos, re-ejecuta la MISMA API interna con
     page.request (misma sesion del navegador), paginando de a 100 hasta traer
     todos los registros (la API topa cada pagina en 100 y expone el total en
     pageable.totalElements).
  4. Respaldo por DOM: si nada de lo anterior captura filas, raspa la tabla.
  5. Deduplica por idContrato, filtra por palabra clave (insensible a
     tildes/mayusculas), clasifica relevancia y exporta a CSV (utf-8-sig).

Uso:
  python scraper_seace_menores.py --keyword token --objeto Servicio \
      --out data/contratos_token.csv [--entidad "ministerio"] [--headed]
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from datetime import datetime, timezone

import pandas as pd
from playwright.sync_api import sync_playwright

URL = "https://prod6.seace.gob.pe/buscador-publico/contrataciones"
API = "https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico/contrataciones/buscador"

# La API exige el parametro 'anio' pero NO filtra por ese anio (2024/2025/2026
# devuelven lo mismo); usamos el anio actual solo para satisfacer el parametro.
ANIO_DEFAULT = datetime.now().year
PAGE_SIZE = 100  # maximo efectivo por pagina que respeta la API

# Objeto de contratacion -> id (de /maestras/listar-objeto-contratacion)
OBJETO_IDS = {
    "bien": 1,
    "servicio": 2,
    "obra": 3,
    "consultoria de obra": 4,
}

# --- Terminos para clasificacion de relevancia -------------------------------
# ALTA: senal fuerte de IA/LLM o el termino "token".
TERMINOS_ALTA = [
    "token", "azure openai", "openai", "gpt", "llm", "claude",
    "copilot", "gemini",
]
# MEDIA/BAJA: terminos genericos de IA (2+ -> MEDIA, 1 -> BAJA).
TERMINOS_GENERICOS = [
    "inteligencia artificial", "ia generativa", "chatbot", "asistente virtual",
    "machine learning", "aprendizaje automatico", "aprendizaje de maquina",
    "procesamiento de lenguaje", "vision computacional", "vision artificial",
    "deep learning", "aprendizaje profundo", "red neuronal", "redes neuronales",
    "modelo de lenguaje", "ciencia de datos", "big data",
]
# Terminos que, por ser subcadenas muy comunes, exigen limites de palabra para
# evitar falsos positivos (p.ej. "ia" dentro de "materia", "gestion", etc.).
_CON_LIMITE = {"ia"}


def normaliza(texto: str) -> str:
    """Minusculas y sin tildes, para matching robusto."""
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", str(texto))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.lower()


def contiene(texto_norm: str, termino: str) -> bool:
    """Coincidencia por subcadena (normalizada). Asi 'token' matchea 'tokens' y
    'gpt' matchea 'chatgpt'. Solo terminos muy ambiguos usan limite de palabra."""
    term = normaliza(termino)
    if term in _CON_LIMITE:
        return re.search(r"\b" + re.escape(term) + r"\b", texto_norm) is not None
    return term in texto_norm


def clasifica(registro: dict) -> tuple[str, str]:
    """Devuelve (relevancia, terminos_detectados)."""
    campos = " ".join(str(registro.get(k, "")) for k in
                      ("desContratacion", "desObjetoContrato", "nomEntidad"))
    tn = normaliza(campos)

    altos = [t for t in TERMINOS_ALTA if contiene(tn, t)]
    genericos = [t for t in TERMINOS_GENERICOS if contiene(tn, t)]

    if altos:
        rel = "ALTA"
    elif len(genericos) >= 2:
        rel = "MEDIA"
    elif len(genericos) == 1:
        rel = "BAJA"
    else:
        rel = "BAJA"
    detectados = ", ".join(dict.fromkeys(altos + genericos))
    return rel, detectados


def matchea_keyword(registro: dict, keyword: str) -> bool:
    """La fila menciona la keyword en alguno de sus campos de texto."""
    kw = normaliza(keyword)
    if not kw:
        return True
    campos = " ".join(str(registro.get(k, "")) for k in
                      ("desContratacion", "desObjetoContrato", "nomEntidad",
                       "nomObjetoContrato"))
    return contiene(normaliza(campos), keyword)


# --- Captura de datos --------------------------------------------------------
def fetch_api(page, keyword: str, objeto_id: int | None, anio: int) -> list[dict]:
    """Via principal: replay directo de la API interna, paginando de a 100."""
    registros: list[dict] = []
    page_num = 1
    total = None
    while True:
        params = {
            "anio": anio,
            "palabra_clave": keyword or "",
            "orden": 2,
            "page": page_num,
            "page_size": PAGE_SIZE,
        }
        if objeto_id:
            params["lista_codigo_objeto"] = objeto_id
        resp = page.request.get(API, params=params, timeout=60000)
        if resp.status != 200:
            print(f"  [api] pagina {page_num} status {resp.status}; corto.")
            break
        data = resp.json()
        lote = data.get("data", []) or []
        if total is None:
            total = data.get("pageable", {}).get("totalElements", 0)
            print(f"  [api] totalElements reportado: {total}")
        registros.extend(lote)
        print(f"  [api] pagina {page_num}: {len(lote)} filas "
              f"(acumulado {len(registros)}/{total})")
        if not lote or len(registros) >= (total or 0) or len(lote) < PAGE_SIZE:
            break
        page_num += 1
        if page_num > 500:  # tope de seguridad
            break
    return registros


def fetch_dom(page) -> list[dict]:
    """Respaldo: raspa la tabla renderizada si la API no dio nada."""
    print("  [dom] intentando respaldo por DOM...")
    try:
        page.wait_for_selector("table tbody tr, .mat-mdc-row", timeout=15000)
    except Exception:
        return []
    filas = page.eval_on_selector_all(
        "table tbody tr",
        """rows => rows.map(r => {
            const c = Array.from(r.querySelectorAll('td')).map(td => td.innerText.trim());
            return c;
        })""",
    )
    registros = []
    for c in filas:
        if not c:
            continue
        registros.append({"desContratacion": " | ".join(c),
                          "desObjetoContrato": " | ".join(c)})
    print(f"  [dom] {len(registros)} filas rescatadas del DOM.")
    return registros


def drive_ui(page, keyword: str, objeto_label: str | None):
    """Maneja el formulario (escribe keyword, marca Objeto, clic Buscar) para
    disparar la API real; los errores no son fatales (la via API es principal)."""
    try:
        inp = page.query_selector("input[placeholder*='Buscar por']")
        if inp:
            inp.click()
            inp.fill(keyword or "")
            print(f"  [ui] escribi keyword: '{keyword}'")
        if objeto_label:
            page.get_by_text(objeto_label, exact=True).first.click()
            print(f"  [ui] marque Objeto: {objeto_label}")
            page.wait_for_timeout(400)
        page.get_by_role("button", name="Buscar").first.click()
        print("  [ui] clic en 'Buscar'")
        page.wait_for_timeout(3500)
    except Exception as e:
        print(f"  [ui] aviso: no se pudo manejar el formulario ({e}); "
              "sigo con la via API.")


def canonical_objeto(objeto: str | None) -> tuple[int | None, str | None]:
    if not objeto:
        return None, None
    key = normaliza(objeto).strip()
    oid = OBJETO_IDS.get(key)
    if oid is None:
        print(f"  [aviso] objeto '{objeto}' no reconocido; se ignora el filtro.")
        return None, None
    # Etiqueta tal como aparece en el checkbox de la pagina.
    label = {1: "Bien", 2: "Servicio", 3: "Obra", 4: "Consultoría de Obra"}[oid]
    return oid, label


def main():
    ap = argparse.ArgumentParser(description="Scraper contrataciones menores SEACE")
    ap.add_argument("--keyword", default="token", help="palabra clave a buscar")
    ap.add_argument("--objeto", default=None,
                    help="Bien | Servicio | Obra | 'Consultoria de Obra'")
    ap.add_argument("--entidad", default=None,
                    help="filtra por nombre de entidad (subcadena)")
    ap.add_argument("--out", default="data/contratos_token.csv",
                    help="ruta del CSV de salida")
    ap.add_argument("--anio", type=int, default=ANIO_DEFAULT,
                    help="anio requerido por la API (no filtra por ese anio)")
    ap.add_argument("--headed", action="store_true",
                    help="abre el navegador visible (para depurar)")
    args = ap.parse_args()

    objeto_id, objeto_label = canonical_objeto(args.objeto)

    print(f"== SEACE scraper == keyword='{args.keyword}' objeto={args.objeto} "
          f"entidad={args.entidad}")

    interceptados: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(ignore_https_errors=True)
        page = ctx.new_page()

        # Intercepta cualquier respuesta JSON del buscador que emita la propia
        # pagina (honra el requerimiento de page.on("response", ...)).
        def on_response(resp):
            if "/buscador?" in resp.url:
                try:
                    interceptados.extend(resp.json().get("data", []) or [])
                except Exception:
                    pass

        page.on("response", on_response)

        print("Cargando SPA...")
        page.goto(URL, wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(2500)

        # Dispara la busqueda real por el formulario (interceptamos su JSON).
        drive_ui(page, args.keyword, objeto_label)

        # Via principal: API directa paginada.
        registros = fetch_api(page, args.keyword, objeto_id, args.anio)

        # Respaldo 1: registros interceptados del formulario.
        if not registros and interceptados:
            print(f"  [fallback] usando {len(interceptados)} registros interceptados.")
            registros = interceptados

        # Respaldo 2: DOM.
        if not registros:
            registros = fetch_dom(page)

        browser.close()

    if not registros:
        print("ERROR: no se capturaron registros.", file=sys.stderr)
        # Igual escribimos un CSV vacio con cabeceras para no romper el flujo.
        registros = []

    # Deduplicar por idContrato (o por la descripcion si no hay id).
    vistos = set()
    unicos = []
    for r in registros:
        clave = r.get("idContrato") or r.get("desContratacion")
        if clave in vistos:
            continue
        vistos.add(clave)
        unicos.append(r)
    print(f"Registros unicos: {len(unicos)}")

    # Filtro por palabra clave (seguridad; la API ya filtra en servidor).
    unicos = [r for r in unicos if matchea_keyword(r, args.keyword)]
    # Filtro opcional por entidad.
    if args.entidad:
        ent = normaliza(args.entidad)
        unicos = [r for r in unicos if ent in normaliza(r.get("nomEntidad", ""))]
    print(f"Tras filtros keyword/entidad: {len(unicos)}")

    # Construir filas del CSV con clasificacion.
    fecha_captura = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    filas = []
    for r in unicos:
        rel, det = clasifica(r)
        filas.append({
            "idContrato": r.get("idContrato"),
            "nroContratacion": r.get("nroContratacion"),
            "desContratacion": r.get("desContratacion"),
            "objeto": r.get("nomObjetoContrato"),
            "descripcion": r.get("desObjetoContrato"),
            "entidad": r.get("nomEntidad"),
            "estado": r.get("nomEstadoContrato"),
            "fecPublica": r.get("fecPublica"),
            "fecIniCotizacion": r.get("fecIniCotizacion"),
            "fecFinCotizacion": r.get("fecFinCotizacion"),
            "relevancia": rel,
            "terminos_detectados": det,
            "keyword": args.keyword,
            "fecha_captura": fecha_captura,
        })

    # Orden: ALTA > MEDIA > BAJA, luego por fecha de publicacion desc.
    orden_rel = {"ALTA": 0, "MEDIA": 1, "BAJA": 2}
    filas.sort(key=lambda f: (orden_rel.get(f["relevancia"], 3),
                              f.get("fecPublica") or ""), reverse=False)

    cols = ["idContrato", "nroContratacion", "desContratacion", "objeto",
            "descripcion", "entidad", "estado", "fecPublica", "fecIniCotizacion",
            "fecFinCotizacion", "relevancia", "terminos_detectados", "keyword",
            "fecha_captura"]
    df = pd.DataFrame(filas, columns=cols)

    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    df.to_csv(args.out, index=False, encoding="utf-8-sig")

    # Resumen.
    print("\n===== RESUMEN =====")
    print(f"CSV: {args.out}  ({len(df)} filas)")
    if len(df):
        print(df["relevancia"].value_counts().to_string())
    print("===================")


if __name__ == "__main__":
    main()
