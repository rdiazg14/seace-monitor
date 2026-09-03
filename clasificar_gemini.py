#!/usr/bin/env python3
"""
Fase B / C1: clasifica categoria_it con Gemini sobre lo que keywords dejo en NULL.

Cascada: SELECT siempre categoria_it IS NULL AND relevancia_ia IS NULL.
No pisa etiquetas de keywords. No toca relevancia_ia. No escribe
flash_ocr_cuota.json.

HERRAMIENTA MANUAL. temperature:0 no es determinista: el dry-run del 1 sep
dijo 1 y la escritura fueron 3 (2 FP Hardware revertidos). Revisar el
SELECT a mano antes de confiar. No meter en pipeline.yml sin Arquitectura C.

C1 (preferido):
    python clasificar_gemini.py --proponer --filtro vigentes
    python clasificar_gemini.py --aplicar data/propuestas_it_YYYYMMDD-HHMMSS.json

Camino directo (deprecado, se elimina en C3):
    python clasificar_gemini.py --dry-run --limit 30 --filtro vigentes
    python clasificar_gemini.py --filtro vigentes            # escribe
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from supabase import create_client

_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

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
GEMINI_BACKOFF = (2.0, 4.0, 8.0, 16.0, 32.0, 64.0)
BATCH_DB = 100
BATCH_P2 = 15
SEED_P2 = 20260902
PAGE_DB = 1_000
TIMEOUT_S = 120.0
DATA_DIR = Path(__file__).parent / "data"
LEDGER_PATH = DATA_DIR / "clasificacion_rechazadas.json"
ARTEFACTO_MAX_DIAS = 7

CATEGORIAS_IT = [
    "Firma digital",
    "IA/analytics",
    "Ciberseguridad",
    "Cloud/hosting",
    "Microsoft",
    "Oracle",
    "Base de datos/ERP",
    "Desarrollo software",
    "Licencias",
    "Soporte tecnico",
    "Redes/cableado",
    "Correo electronico",
    "Hardware",
]
CATEGORIA_NINGUNA = "ninguna"
ENUM_CATEGORIA = CATEGORIAS_IT + [CATEGORIA_NINGUNA]
CONF_ENUM = ("alta", "media", "baja")

RESPONSE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "categoria": {"type": "string", "enum": ENUM_CATEGORIA},
        },
        "required": ["id", "categoria"],
    },
}

RESPONSE_SCHEMA_P1 = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "senal": {"type": "string"},
            "categoria": {"type": "string", "enum": ENUM_CATEGORIA},
            "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
        },
        "required": ["id", "senal", "categoria", "confianza"],
        "propertyOrdering": ["id", "senal", "categoria", "confianza"],
    },
}

RESPONSE_SCHEMA_P2 = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "senal": {"type": "string"},
            "categoria": {"type": "string", "enum": ENUM_CATEGORIA},
        },
        "required": ["id", "senal", "categoria"],
        "propertyOrdering": ["id", "senal", "categoria"],
    },
}

# Una linea por categoria, derivada de IT_CATS (ingesta_completa.py).
DEF_CATEGORIAS = """\
- Firma digital: firma digital, certificado digital/electronico, token criptografico.
- IA/analytics: inteligencia artificial, LLM/GPT/Copilot, analytics, BI, big data, ML.
- Ciberseguridad: ciberseguridad, seguridad informatica/de la informacion, firewall, pentest.
- Cloud/hosting: nube publica, cloud computing, hosting, servidor virtual, AWS, Google Cloud.
- Microsoft: Microsoft 365, Office 365, SharePoint, Exchange, Windows Server.
- Oracle: Oracle Database, Oracle EBS, PeopleSoft.
- Base de datos/ERP: motores SQL, data warehouse, SAP, ERP.
- Desarrollo software: creacion/implementacion de sistemas, aplicativos web o moviles, software a medida.
- Licencias: licencia, licenciamiento o suscripcion de software.
- Soporte tecnico: soporte tecnico, mantenimiento de software/sistemas, mesa de ayuda.
- Redes/cableado: red de datos, cableado estructurado, switch, router, wifi, fibra optica.
- Correo electronico: correo o mensajeria electronica.
- Hardware: compra de EQUIPOS de computo (PC, laptop, impresora, monitor, disco, RAM, scanner, UPS de datacenter). NO son Hardware: los consumibles y suministros de esos equipos (toner, cartuchos, tinta, cintas, papel, etiquetas, rollos, repuestos genericos) aunque el texto nombre el equipo que los usa; ni los equipos de reprografia de oficina (fotocopiadora, duplicadora, mimeografo, guillotina); ni electrodomesticos, estabilizadores de oficina o aire acondicionado. Todo eso -> 'ninguna'.
- ninguna: el objeto NO es tecnologia de la informacion.
"""

SYSTEM_PROMPT_REGLAS = (
    "Eres un clasificador de contratos publicos peruanos para una "
    "empresa de TI (ENERTRONIC: IA, cloud, desarrollo de software, servicios TI). "
    "Clasificas cada contrato en UNA de 13 categorias IT, o 'ninguna' si NO es "
    "un contrato de tecnologia de la informacion.\n"
    "REGLA CRITICA: clasifica por el OBJETO real del contrato (que se compra o "
    "contrata), NO por el area que lo solicita. Un area de TI/Informatica que "
    "compra aire acondicionado, mobiliario, estabilizadores o pide personal "
    "administrativo NO es un contrato IT -> 'ninguna'. Un area no-TI que compra "
    "desarrollo de software SI es IT.\n"
    "Personal: locar o contratar a una persona (bachiller, ingeniero, locador, "
    "practicante, 'servicio de un profesional') NO es Desarrollo software aunque "
    "el titulo sea de sistemas/informatica. Desarrollo software es crear o "
    "implementar un sistema/aplicativo/software, no alquilar un profesional.\n"
    "Hardware es SOLO compra de EQUIPOS de computo (PC, laptop, impresora, "
    "monitor, disco, RAM, scanner, UPS de datacenter). NO son Hardware: los "
    "consumibles y suministros de esos equipos (toner, cartuchos, tinta, "
    "cintas, papel, etiquetas, rollos, repuestos genericos) aunque el texto "
    "nombre el equipo que los usa; ni los equipos de reprografia de oficina "
    "(fotocopiadora, duplicadora, mimeografo, guillotina); ni electrodomesticos, "
    "estabilizadores de oficina o aire acondicionado. Todo eso -> 'ninguna'.\n"
    'La "senal" debe copiarse del texto de "descripcion", "objeto" o "item". El '
    'campo "cubso" es la familia del catalogo estatal, no describe lo que se '
    'compra: si la unica evidencia esta ahi, la confianza es "media" como maximo.\n'
    "Definicion breve de cada categoria:\n"
    f"{DEF_CATEGORIAS}"
    "Ante la duda entre una categoria IT y 'ninguna', prefiere 'ninguna' si el "
    "objeto no es claramente tecnologia (mejor no clasificar que clasificar mal).\n"
)

SYSTEM_PROMPT_JSON = (
    "Responde solo el JSON array del schema, un objeto por contrato de entrada."
)

BLOQUE_CONFIANZA_P1 = (
    "Por cada contrato devuelve tambien:\n"
    '- "senal": las palabras LITERALES del texto del contrato (descripcion, objeto o '
    "item) que justifican la categoria, copiadas tal cual, maximo 60 caracteres. "
    "No inventes ni parafrasees. Si no podes copiar palabras del texto que "
    'justifiquen la categoria, la confianza es "baja".\n'
    '- "confianza": "alta" si el texto nombra explicitamente el producto o servicio '
    'de la categoria; "media" si se infiere del contexto pero no esta nombrado; '
    '"baja" si podria ser otra categoria o \'ninguna\'.\n'
    'Para \'ninguna\' usa siempre confianza "alta" y senal "".\n'
)

LINEA_P2_CIEGO = (
    "Clasificas solo por el objeto del contrato. No tenes informacion de la entidad "
    "ni del area solicitante y no debes suponerla.\n"
)

SYSTEM_PROMPT = SYSTEM_PROMPT_REGLAS + SYSTEM_PROMPT_JSON
SYSTEM_PROMPT_P1 = SYSTEM_PROMPT_REGLAS + BLOQUE_CONFIANZA_P1 + SYSTEM_PROMPT_JSON
SYSTEM_PROMPT_P2 = SYSTEM_PROMPT_REGLAS + LINEA_P2_CIEGO + SYSTEM_PROMPT_JSON

COLS = (
    "id,descripcion,descripcion_contrato,objeto,entidad,"
    "nom_area_usuaria,items_json,categoria_it,relevancia_ia,"
    "estado,fecha_fin_cotizacion"
)

TOKEN_STATS = {
    "prompt": 0,
    "candidates": 0,
    "total": 0,
    "llamadas": 0,
}


def reset_token_stats() -> None:
    for k in TOKEN_STATS:
        TOKEN_STATS[k] = 0


def acumular_tokens(body: dict) -> None:
    um = body.get("usageMetadata") or {}

    def n(key: str) -> int:
        try:
            return int(um.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    TOKEN_STATS["prompt"] += n("promptTokenCount")
    TOKEN_STATS["candidates"] += n("candidatesTokenCount")
    TOKEN_STATS["total"] += n("totalTokenCount")
    TOKEN_STATS["llamadas"] += 1


def init_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY no encontrados",
              flush=True)
        return None
    try:
        client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        print("[supabase] cliente inicializado OK", flush=True)
        return client
    except Exception as e:
        print(f"ERROR: no se pudo conectar a Supabase: {e}", flush=True)
        return None


def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def pasa_filtro(
    row: dict,
    filtro: str,
    now: datetime,
    *,
    incluir_ventana_cerrada: bool = False,
) -> bool:
    est = row.get("estado") or ""
    # Clasificar cuesta ~200 tokens por contrato; el filtro de ventana existe
    # para el OCR (Flash sobre PDF), no para esto. Con 27% de ventanas bajo
    # 24 h (B12), la etiqueta tiene que existir ANTES de que la ventana abra.
    # Ademas fecha_fin_cotizacion esta bajo sospecha de corrimiento horario (B21).
    if incluir_ventana_cerrada:
        vigente_ok = est == "Vigente"
    else:
        fin = _parse_dt(row.get("fecha_fin_cotizacion"))
        vigente_ok = est == "Vigente" and (fin is None or fin >= now)
    if filtro == "todos":
        return True
    if filtro == "vigentes":
        return vigente_ok
    if filtro == "evaluacion":
        return vigente_ok or est == "En Evaluacion" or est == "En Evaluación"
    return False


def cubsos(row: dict) -> str:
    items = row.get("items_json") or []
    if not isinstance(items, list):
        return ""
    names: list[str] = []
    for it in items[:8]:
        if not isinstance(it, dict):
            continue
        n = (it.get("nom_cubso") or it.get("descripcion") or "").strip()
        if n:
            names.append(n[:80])
    return "; ".join(names)


# nom_cubso es la familia del catalogo estatal bajo la que se compra; nombra
# el equipo aunque se compre el consumible. La descripcion del item es lo que
# realmente se adquiere. cubsos() prefiere nom_cubso y por eso TAPA la
# descripcion del item (caso 90386: nom_cubso "PROCESADOR DE PUERTA DE ENLACE
# DE VIGILANCIA..." ocultando "CPU P/AUTOMATA 63E/S").
def items_desc(row: dict) -> str:
    items = row.get("items_json") or []
    if not isinstance(items, list):
        return ""
    names: list[str] = []
    for it in items[:8]:
        if not isinstance(it, dict):
            continue
        n = (it.get("descripcion") or "").strip()
        if n:
            names.append(n[:80])
    return "; ".join(names)


def items_cubso(row: dict) -> str:
    items = row.get("items_json") or []
    if not isinstance(items, list):
        return ""
    names: list[str] = []
    for it in items[:8]:
        if not isinstance(it, dict):
            continue
        n = (it.get("nom_cubso") or "").strip()
        if n:
            names.append(n[:80])
    return "; ".join(names)


def recortar(s, n: int = 220) -> str:
    t = " ".join(str(s or "").split())
    return t if len(t) <= n else t[: n - 1] + "..."


def normalizar(s: str) -> str:
    t = unicodedata.normalize("NFKD", str(s or ""))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.lower()
    t = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in t)
    return " ".join(t.split()).strip()


def verificar_senal(senal: str, row: dict) -> tuple[bool, str]:
    """Devuelve (verificada, fuente). fuente in
    'descripcion'|'objeto'|'item'|'cubso'|'ninguna'."""
    ns = normalizar(senal)
    if len(ns) < 4:
        return False, "ninguna"
    candidatos = (
        ("descripcion", recortar(row.get("descripcion"), 400)),
        ("objeto", recortar(row.get("objeto"), 80)),
        ("item", recortar(items_desc(row), 240)),
        ("cubso", recortar(items_cubso(row), 240)),
    )
    for fuente, texto in candidatos:
        if ns in normalizar(texto):
            return True, fuente
    return False, "ninguna"


def verificar_senal_p2(senal: str, row: dict) -> tuple[bool, str]:
    """Igual que verificar_senal pero sin cubso: P2 no vio ese campo."""
    ns = normalizar(senal)
    if len(ns) < 4:
        return False, "ninguna"
    candidatos = (
        ("descripcion", recortar(row.get("descripcion"), 400)),
        ("objeto", recortar(row.get("objeto"), 80)),
        ("item", recortar(items_desc(row), 240)),
    )
    for fuente, texto in candidatos:
        if ns in normalizar(texto):
            return True, fuente
    return False, "ninguna"


def degradar_p1(p: dict, row: dict) -> None:
    """Post-proceso P1: verifica senal y degrada confianza. Mutates p."""
    p["confianza_original"] = p["confianza"]
    if p["categoria"] == CATEGORIA_NINGUNA:
        p["senal_verificada"] = True
        p["senal_fuente"] = "ninguna"
        return
    ok, fuente = verificar_senal(p.get("senal") or "", row)
    p["senal_verificada"] = ok
    p["senal_fuente"] = fuente
    # CUBSO es la familia de catalogo bajo la que se compra, no el objeto
    # del contrato. Nombra el equipo aunque se compre el consumible. No es
    # evidencia suficiente para escribir directo.
    if not ok:
        p["confianza"] = "baja"
    if fuente == "cubso" and p["confianza"] == "alta":
        p["confianza"] = "media"


def anexar_verificacion_p2(p: dict, row: dict) -> None:
    if p["categoria"] == CATEGORIA_NINGUNA:
        p["senal_verificada"] = True
        p["senal_fuente"] = "ninguna"
        return
    ok, fuente = verificar_senal_p2(p.get("senal") or "", row)
    p["senal_verificada"] = ok
    p["senal_fuente"] = fuente


def paginar_nulls(
    supa,
    filtro: str,
    limit: int,
    *,
    incluir_ventana_cerrada: bool = False,
) -> list[dict]:
    """categoria_it IS NULL AND relevancia_ia IS NULL + filtro de universo."""
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    offset = 0
    while True:
        q = (
            supa.table("contratos")
            .select(COLS)
            .is_("categoria_it", "null")
            .is_("relevancia_ia", "null")
            .order("id", desc=True)
        )
        if filtro == "vigentes":
            q = q.eq("estado", "Vigente")
        elif filtro == "evaluacion":
            q = q.in_("estado", ["Vigente", "En Evaluación", "En Evaluacion"])
        res = q.range(offset, offset + PAGE_DB - 1).execute()
        batch = res.data or []
        for row in batch:
            if not pasa_filtro(
                row, filtro, now,
                incluir_ventana_cerrada=incluir_ventana_cerrada,
            ):
                continue
            out.append(row)
            if limit and len(out) >= limit:
                break
        print(f"  leidos {len(out):,}", flush=True)
        if len(batch) < PAGE_DB:
            break
        if limit and len(out) >= limit:
            break
        offset += PAGE_DB
    return out[:limit] if limit else out


def extract_gemini_text(body: dict) -> str:
    parts = (
        (body.get("candidates") or [{}])[0]
        .get("content", {})
        .get("parts") or []
    )
    textos = [p.get("text") or "" for p in parts if not p.get("thought")]
    return "".join(textos).strip()


def parse_array(text: str) -> list[dict]:
    s = text.strip().replace("```json", "").replace("```", "").strip()
    raw = json.loads(s)
    if isinstance(raw, dict):
        for v in raw.values():
            if isinstance(v, list):
                raw = v
                break
    if not isinstance(raw, list):
        raise RuntimeError(f"gemini no devolvio array (type={type(raw).__name__})")
    return raw


def user_prompt(lote: list[dict]) -> str:
    lineas = [
        "Clasifica estos contratos. Devuelve un JSON array con un objeto "
        "{id, categoria} por cada id de entrada.",
        "",
    ]
    for i, row in enumerate(lote, 1):
        lineas.append(f"{i}. id={row['id']}")
        lineas.append(f"   descripcion: {recortar(row.get('descripcion'), 400)}")
        lineas.append(f"   objeto: {recortar(row.get('objeto'), 80)}")
        lineas.append(f"   item: {recortar(items_desc(row), 240)}")
        lineas.append(f"   cubso: {recortar(items_cubso(row), 240)}")
        lineas.append(f"   entidad: {recortar(row.get('entidad'), 120)}")
        lineas.append(f"   area_usuaria: {recortar(row.get('nom_area_usuaria'), 160)}")
        lineas.append("")
    return "\n".join(lineas)


def user_prompt_p2(lote: list[dict]) -> str:
    lineas = [
        "Clasifica estos contratos. Devuelve un JSON array con un objeto "
        "{id, senal, categoria} por cada id de entrada.",
        "",
    ]
    for i, row in enumerate(lote, 1):
        lineas.append(f"{i}. id={row['id']}")
        lineas.append(f"   descripcion: {recortar(row.get('descripcion'), 400)}")
        lineas.append(f"   objeto: {recortar(row.get('objeto'), 80)}")
        lineas.append(f"   item: {recortar(items_desc(row), 240)}")
        lineas.append("")
    return "\n".join(lineas)


def clasificar_lote(
    client: httpx.Client,
    lote: list[dict],
    *,
    system_prompt: str | None = None,
    schema: dict | None = None,
    armar_prompt=None,
) -> list[dict]:
    sys_p = SYSTEM_PROMPT if system_prompt is None else system_prompt
    sch = RESPONSE_SCHEMA if schema is None else schema
    fn = user_prompt if armar_prompt is None else armar_prompt
    payload = {
        "system_instruction": {"parts": [{"text": sys_p}]},
        "contents": [{"role": "user", "parts": [{"text": fn(lote)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": sch,
            "thinkingConfig": {"thinkingLevel": "LOW"},
            "temperature": 0,
            "maxOutputTokens": 8192,
        },
    }
    waits = [0.0] + list(GEMINI_BACKOFF)
    last_err: Exception | None = None
    for attempt, wait in enumerate(waits):
        if wait:
            print(f"    [gemini backoff {wait:.0f}s attempt={attempt}]", flush=True)
            time.sleep(wait)
        try:
            r = client.post(
                GEMINI_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": GEMINI_API_KEY,
                },
                json=payload,
                timeout=TIMEOUT_S,
            )
            if r.status_code == 429:
                last_err = RuntimeError(f"429 {r.text[:200]}")
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        extra = min(float(retry_after), 120.0)
                        print(f"    [429 Retry-After {extra:.0f}s]", flush=True)
                        time.sleep(extra)
                    except ValueError:
                        pass
                continue
            r.raise_for_status()
            body = r.json()
            acumular_tokens(body)
            text = extract_gemini_text(body)
            if not text:
                raise RuntimeError("gemini vacio")
            return parse_array(text)
        except httpx.HTTPStatusError as e:
            last_err = e
            code = e.response.status_code if e.response is not None else 0
            if e.response is not None and e.response.status_code in (429, 500, 503):
                print(f"    [retry {attempt}] HTTP {code}", flush=True)
                continue
            raise
        except Exception as e:
            last_err = e
            print(f"    [retry {attempt}] {e}", flush=True)
    raise RuntimeError(f"clasificar_lote fallo: {last_err}")


def aplicar_respuestas(
    lote: list[dict],
    raw: list[dict],
) -> list[tuple[dict, str]]:
    """(row, categoria_enum) incluyendo 'ninguna'. Ignora ids ajenos."""
    by_id = {int(r["id"]): r for r in lote}
    vistos: set[int] = set()
    out: list[tuple[dict, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            cid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if cid not in by_id or cid in vistos:
            continue
        cat = str(item.get("categoria") or "").strip()
        if cat not in ENUM_CATEGORIA:
            print(f"    [aviso] id={cid} categoria fuera de enum: {cat!r} -> ninguna",
                  flush=True)
            cat = CATEGORIA_NINGUNA
        vistos.add(cid)
        out.append((by_id[cid], cat))
    for cid, row in by_id.items():
        if cid not in vistos:
            print(f"    [aviso] id={cid} ausente en respuesta Gemini -> ninguna",
                  flush=True)
            out.append((row, CATEGORIA_NINGUNA))
    return out


def emparejar_lote(
    lote: list[dict],
    raw: list[dict],
) -> tuple[dict[int, dict], list[int]]:
    """Ids del lote vs respuesta. No rellena ausentes con ninguna."""
    enviados = {int(r["id"]) for r in lote}
    vistos: set[int] = set()
    matched: dict[int, dict] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            cid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if cid not in enviados:
            print(f"    [aviso] id={cid} fuera del lote", flush=True)
            continue
        if cid in vistos:
            print(
                f"    [aviso] id={cid} duplicado en respuesta, se descarta el segundo",
                flush=True,
            )
            continue
        vistos.add(cid)
        matched[cid] = item
    missing = [int(r["id"]) for r in lote if int(r["id"]) not in vistos]
    for cid in missing:
        print(f"    [aviso] id={cid} ausente en respuesta Gemini -> sin_respuesta",
              flush=True)
    return matched, missing


def parse_p1_item(item: dict) -> dict | None:
    cat = str(item.get("categoria") or "").strip()
    if cat not in ENUM_CATEGORIA:
        return None
    senal = str(item.get("senal") or "")[:60]
    if cat == CATEGORIA_NINGUNA:
        return {"categoria": cat, "confianza": "alta", "senal": ""}
    conf = str(item.get("confianza") or "").strip().lower()
    if conf not in CONF_ENUM:
        conf = "baja"
    return {"categoria": cat, "confianza": conf, "senal": senal}


def parse_p2_item(item: dict) -> dict | None:
    cat = str(item.get("categoria") or "").strip()
    if cat not in ENUM_CATEGORIA:
        return None
    senal = str(item.get("senal") or "")[:60]
    if cat == CATEGORIA_NINGUNA:
        senal = ""
    return {"categoria": cat, "senal": senal}


def flush_upsert(supa, lote: list[dict]) -> None:
    if not lote:
        return
    supa.table("contratos").upsert(lote, on_conflict="id").execute()
    print(f"    upsert lote {len(lote)} filas OK", flush=True)


def cargar_ledger() -> list[dict]:
    if not LEDGER_PATH.exists():
        return []
    data = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def ledger_por_id(ledger: list[dict]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for e in ledger:
        if not isinstance(e, dict):
            continue
        try:
            cid = int(e.get("id"))
        except (TypeError, ValueError):
            continue
        out.setdefault(cid, []).append(e)
    return out


def categoria_propuesta_escritura(item: dict) -> str | None:
    if item.get("decision") != "escribir":
        return None
    if item.get("origen") == "desempate_ok" and isinstance(item.get("p2"), dict):
        cat = item["p2"].get("categoria")
    else:
        p1 = item.get("p1") or {}
        cat = p1.get("categoria")
    if cat in CATEGORIAS_IT:
        return cat
    return None


def aplicar_ledger(items: list[dict], ledger: list[dict]) -> None:
    por_id = ledger_por_id(ledger)
    for item in items:
        cid = int(item["id"])
        entradas = por_id.get(cid, [])
        item["en_ledger"] = bool(entradas)
        cat_prop = categoria_propuesta_escritura(item)
        if not cat_prop or not entradas:
            continue
        for e in entradas:
            if e.get("categoria_rechazada") == cat_prop:
                item["decision"] = "rechazado_previo"
                print(
                    f"[LEDGER] id={cid} vuelve a proponer {cat_prop} "
                    f"(rechazada el {e.get('fecha')})",
                    flush=True,
                )
                break


def conteos_items(items: list[dict]) -> dict[str, int]:
    c = {
        "alta_directa": 0,
        "desempate_ok": 0,
        "desempate_discrepa": 0,
        "ninguna": 0,
        "sin_respuesta": 0,
        "rechazado_previo": 0,
        "senal_no_verificada": 0,
        "senal_solo_cubso": 0,
        "senal_fuente_item": 0,
        "degradados_a_p2": 0,
        "desempate_sin_evidencia": 0,
        "p2_senal_no_verificada": 0,
    }
    for it in items:
        dec = it.get("decision")
        orig = it.get("origen")
        if dec == "rechazado_previo":
            c["rechazado_previo"] += 1
        elif dec == "sin_respuesta":
            c["sin_respuesta"] += 1
        elif orig == "ninguna":
            c["ninguna"] += 1
        elif orig == "alta_directa":
            c["alta_directa"] += 1
        elif orig == "desempate_ok":
            c["desempate_ok"] += 1
        elif orig == "desempate_sin_evidencia":
            c["desempate_sin_evidencia"] += 1
        elif orig == "desempate_discrepa":
            c["desempate_discrepa"] += 1
        p1 = it.get("p1") or {}
        cat_p1 = p1.get("categoria")
        if cat_p1 and cat_p1 != CATEGORIA_NINGUNA:
            if p1.get("senal_verificada") is False:
                c["senal_no_verificada"] += 1
            if p1.get("senal_fuente") == "cubso":
                c["senal_solo_cubso"] += 1
            if p1.get("senal_fuente") == "item":
                c["senal_fuente_item"] += 1
            if (
                p1.get("confianza_original") == "alta"
                and p1.get("confianza") in ("media", "baja")
            ):
                c["degradados_a_p2"] += 1
        p2 = it.get("p2") or {}
        if p2.get("senal_verificada") is False:
            c["p2_senal_no_verificada"] += 1
    return c


def ruta_artefacto(ahora: datetime) -> Path:
    stamp = ahora.strftime("%Y%m%d-%H%M%S")
    return DATA_DIR / f"propuestas_it_{stamp}.json"


def escribir_json(path: Path, payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def correr_pasada(
    client: httpx.Client,
    filas: list[dict],
    batch: int,
    *,
    etiqueta: str,
    system_prompt: str,
    schema: dict,
    armar_prompt,
) -> tuple[dict[int, dict], set[int]]:
    matched_out: dict[int, dict] = {}
    sin: set[int] = set()
    n_lotes = -(-len(filas) // batch) if filas else 0
    parse = parse_p1_item if etiqueta == "P1" else parse_p2_item
    for i in range(0, len(filas), batch):
        lote = filas[i: i + batch]
        num = i // batch + 1
        print(f"  lote {etiqueta} {num}/{n_lotes} n={len(lote)}", flush=True)
        try:
            raw = clasificar_lote(
                client,
                lote,
                system_prompt=system_prompt,
                schema=schema,
                armar_prompt=armar_prompt,
            )
        except Exception as e:
            print(f"  [lote {etiqueta} fallo] {e}", flush=True)
            for row in lote:
                sin.add(int(row["id"]))
            continue
        matched, missing = emparejar_lote(lote, raw)
        sin.update(missing)
        for cid, item in matched.items():
            parsed = parse(item)
            if parsed is None:
                print(
                    f"    [aviso] id={cid} {etiqueta} categoria invalida "
                    f"{item.get('categoria')!r} -> sin_respuesta",
                    flush=True,
                )
                sin.add(cid)
            else:
                matched_out[cid] = parsed
    return matched_out, sin


def comando_proponer(supa, args, filas: list[dict]) -> int:
    reset_token_stats()
    por_id = {int(r["id"]): r for r in filas}
    p1_map: dict[int, dict] = {}
    sin_p1: set[int] = set()
    p2_map: dict[int, dict] = {}
    sin_p2: set[int] = set()

    with httpx.Client() as client:
        if filas:
            p1_map, sin_p1 = correr_pasada(
                client,
                filas,
                args.batch,
                etiqueta="P1",
                system_prompt=SYSTEM_PROMPT_P1,
                schema=RESPONSE_SCHEMA_P1,
                armar_prompt=user_prompt,
            )
            for cid, p in p1_map.items():
                degradar_p1(p, por_id[cid])
            ids_p2 = [
                cid
                for cid, p in p1_map.items()
                if p["categoria"] != CATEGORIA_NINGUNA
                and p["confianza"] in ("media", "baja")
            ]
            rng = random.Random(SEED_P2)
            rng.shuffle(ids_p2)
            print(
                f"  pasada 2 candidatos={len(ids_p2)} batch={BATCH_P2} seed={SEED_P2}",
                flush=True,
            )
            filas_p2 = [por_id[cid] for cid in ids_p2]
            if filas_p2:
                p2_map, sin_p2 = correr_pasada(
                    client,
                    filas_p2,
                    BATCH_P2,
                    etiqueta="P2",
                    system_prompt=SYSTEM_PROMPT_P2,
                    schema=RESPONSE_SCHEMA_P2,
                    armar_prompt=user_prompt_p2,
                )

    items: list[dict] = []
    for row in filas:
        cid = int(row["id"])
        item = {
            "id": cid,
            "descripcion": recortar(row.get("descripcion"), 120),
            "entidad": recortar(row.get("entidad"), 80),
            "p1": None,
            "p2": None,
            "decision": "sin_respuesta",
            "origen": None,
            "en_ledger": False,
        }
        if cid in sin_p1 or cid not in p1_map:
            items.append(item)
            continue
        p1 = p1_map[cid]
        item["p1"] = p1
        if p1["categoria"] == CATEGORIA_NINGUNA:
            item["decision"] = "no_escribir"
            item["origen"] = "ninguna"
        elif p1["confianza"] == "alta":
            item["decision"] = "escribir"
            item["origen"] = "alta_directa"
        elif cid in sin_p2 or cid not in p2_map:
            item["decision"] = "sin_respuesta"
            item["origen"] = None
        else:
            p2 = p2_map[cid]
            anexar_verificacion_p2(p2, row)
            item["p2"] = p2
            if p1["categoria"] == p2["categoria"]:
                if p2.get("senal_verificada"):
                    item["decision"] = "escribir"
                    item["origen"] = "desempate_ok"
                else:
                    item["decision"] = "cola"
                    item["origen"] = "desempate_sin_evidencia"
            else:
                item["decision"] = "cola"
                item["origen"] = "desempate_discrepa"
        items.append(item)

    aplicar_ledger(items, cargar_ledger())
    counts = conteos_items(items)
    ahora = datetime.now(timezone.utc)
    payload = {
        "meta": {
            "generado_utc": ahora.isoformat(),
            "modelo": GEMINI_FLASH,
            "filtro": args.filtro,
            "limit": args.limit,
            "batch_p1": args.batch,
            "batch_p2": BATCH_P2,
            "universo_seleccionado": len(filas),
            "version_c": "C1.2",
            "incluir_ventana_cerrada": bool(
                getattr(args, "incluir_ventana_cerrada", False)
            ),
            "tokens": {
                "prompt": TOKEN_STATS["prompt"],
                "candidates": TOKEN_STATS["candidates"],
                "total": TOKEN_STATS["total"],
                "llamadas": TOKEN_STATS["llamadas"],
            },
            "conteos": counts,
        },
        "items": items,
    }
    path = ruta_artefacto(ahora)
    escribir_json(path, payload)

    print("\n  id | decision | origen | p1 | p2 | descripcion", flush=True)
    for it in items:
        p1c = (it.get("p1") or {}).get("categoria") or "-"
        p2c = (it.get("p2") or {}).get("categoria") or "-"
        print(
            f"  {it['id']} | {it['decision']} | {it.get('origen') or '-'} | "
            f"{p1c} | {p2c} | {it['descripcion']}",
            flush=True,
        )
    print("\n--- resumen C1 --proponer ---", flush=True)
    print(f"  artefacto={path}", flush=True)
    print(f"  universo={len(filas)}", flush=True)
    for k, n in counts.items():
        print(f"    {k}: {n}", flush=True)
    print(
        f"  tokens prompt={TOKEN_STATS['prompt']} "
        f"candidates={TOKEN_STATS['candidates']} "
        f"total={TOKEN_STATS['total']} llamadas={TOKEN_STATS['llamadas']}",
        flush=True,
    )
    print("  no se escribio en Supabase.", flush=True)
    if counts["sin_respuesta"] > 0:
        print(
            f"ERROR: sin_respuesta={counts['sin_respuesta']} "
            "(corrida incompleta; artefacto escrito)",
            flush=True,
        )
        return 3
    return 0


def reselect_ids(supa, ids: list[int]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for i in range(0, len(ids), BATCH_DB):
        chunk = ids[i: i + BATCH_DB]
        res = (
            supa.table("contratos")
            .select("id,categoria_it,relevancia_ia")
            .in_("id", chunk)
            .execute()
        )
        for row in res.data or []:
            out[int(row["id"])] = row
    return out


def comando_aplicar(ruta: str) -> int:
    path = Path(ruta)
    if not path.is_file():
        print(f"ERROR: no existe el artefacto {path}", flush=True)
        return 1
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        print("ERROR: artefacto no es un objeto JSON", flush=True)
        return 1
    if payload.get("aplicado"):
        print(
            "ERROR: este artefacto ya se aplico (bloque aplicado). No re-aplicar.",
            flush=True,
        )
        return 5
    meta = payload.get("meta") or {}
    gen = _parse_dt(meta.get("generado_utc"))
    if gen is None:
        print("ERROR: meta.generado_utc ausente o invalido.", flush=True)
        return 1
    edad = datetime.now(timezone.utc) - gen
    if edad > timedelta(days=ARTEFACTO_MAX_DIAS):
        print(
            f"ERROR: el artefacto tiene mas de {ARTEFACTO_MAX_DIAS} dias "
            f"(generado_utc={meta.get('generado_utc')}). "
            "El estado de la BD ya no es el del SELECT original.",
            flush=True,
        )
        return 4

    items = payload.get("items") or []
    a_escribir: list[dict] = []
    for it in items:
        if not isinstance(it, dict) or it.get("decision") != "escribir":
            continue
        cat = categoria_propuesta_escritura(it)
        if not cat:
            print(
                f"    [aviso] id={it.get('id')} decision=escribir sin categoria IT; skip",
                flush=True,
            )
            continue
        a_escribir.append({"id": int(it["id"]), "categoria_it": cat})

    n_propuestos = len(a_escribir)
    print(
        f"  --aplicar {path.name}  decision=escribir {n_propuestos}",
        flush=True,
    )

    supa = init_supabase()
    if supa is None:
        return 1

    actuales = reselect_ids(supa, [p["id"] for p in a_escribir])
    pendientes: list[dict] = []
    descartados: list[int] = []
    for p in a_escribir:
        cid = p["id"]
        row = actuales.get(cid)
        if row is None:
            print(
                f"[skip] id={cid} no aparece en el re-SELECT",
                flush=True,
            )
            descartados.append(cid)
            continue
        ya = row.get("categoria_it") or row.get("relevancia_ia")
        if row.get("categoria_it") or row.get("relevancia_ia"):
            print(
                f"[skip] id={cid} ya etiquetado como {ya} desde el SELECT original",
                flush=True,
            )
            descartados.append(cid)
            continue
        pendientes.append(p)

    escritos_ids: list[int] = []
    lote: list[dict] = []
    for p in pendientes:
        lote.append(p)
        if len(lote) >= BATCH_DB:
            flush_upsert(supa, lote)
            escritos_ids.extend(int(x["id"]) for x in lote)
            lote.clear()
    if lote:
        flush_upsert(supa, lote)
        escritos_ids.extend(int(x["id"]) for x in lote)

    payload["aplicado"] = {
        "utc": datetime.now(timezone.utc).isoformat(),
        "escritos": escritos_ids,
        "descartados": descartados,
    }
    escribir_json(path, payload)

    print("\n--- resumen C1 --aplicar ---", flush=True)
    print(f"  propuestos={n_propuestos}", flush=True)
    print(f"  descartados por re-select={len(descartados)}", flush=True)
    print(f"  escritos={len(escritos_ids)}", flush=True)
    return 0


def camino_directo(supa, args, filas: list[dict]) -> int:
    ids_lote = {int(r["id"]) for r in filas}
    print(f"  ids en SELECT: {len(ids_lote)} (cascada: solo nulls)", flush=True)

    resultados: list[tuple[dict, str]] = []
    with httpx.Client() as client:
        n_lotes = -(-len(filas) // args.batch)
        for i in range(0, len(filas), args.batch):
            lote = filas[i: i + args.batch]
            num = i // args.batch + 1
            print(f"  lote Gemini {num}/{n_lotes} n={len(lote)}", flush=True)
            raw = clasificar_lote(client, lote)
            resultados.extend(aplicar_respuestas(lote, raw))

    cats = Counter(cat for _, cat in resultados)
    n_ninguna = cats.get(CATEGORIA_NINGUNA, 0)
    n_it = len(resultados) - n_ninguna

    print("\n  id | categoria | descripcion", flush=True)
    for row, cat in resultados:
        desc = recortar(row.get("descripcion"), 100)
        print(f"  {row['id']} | {cat} | {desc}", flush=True)

    print("\n--- resumen ---", flush=True)
    print(f"  procesados={len(resultados)}", flush=True)
    print(f"  IT={n_it}  ninguna={n_ninguna}", flush=True)
    print("  por categoria:", flush=True)
    for k, n in cats.most_common():
        print(f"    {k}: {n}", flush=True)

    a_escribir = [
        {"id": int(row["id"]), "categoria_it": cat}
        for row, cat in resultados
        if cat in CATEGORIAS_IT
    ]
    print(f"  escribirian={len(a_escribir)}  (ninguna se deja NULL)", flush=True)

    if args.dry_run:
        print(f"\n[dry-run] no se escribio. {len(a_escribir)} UPDATE pendientes.",
              flush=True)
        return 0

    pendiente: list[dict] = []
    for p in a_escribir:
        pendiente.append(p)
        if len(pendiente) >= BATCH_DB:
            flush_upsert(supa, pendiente)
            pendiente.clear()
    flush_upsert(supa, pendiente)
    print(f"escritos: {len(a_escribir):,}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fase B/C1: categoria_it con Gemini (solo nulls de keywords)"
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Camino directo: clasifica y muestra; no escribe")
    ap.add_argument("--proponer", action="store_true",
                    help="C1: dos pasadas Gemini -> artefacto JSON; no escribe")
    ap.add_argument("--aplicar", metavar="RUTA", default=None,
                    help="C1: aplica un artefacto --proponer; no llama Gemini")
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
    args = ap.parse_args()

    if args.proponer and args.aplicar:
        print("ERROR: --proponer y --aplicar son mutuamente excluyentes",
              flush=True)
        return 2
    if args.proponer and args.dry_run:
        print("ERROR: --proponer y --dry-run son mutuamente excluyentes",
              flush=True)
        return 2
    if args.aplicar and args.dry_run:
        print("ERROR: --aplicar y --dry-run son mutuamente excluyentes",
              flush=True)
        return 2
    if not args.proponer and not args.aplicar:
        print(
            "[deprecado] usa --proponer / --aplicar; "
            "el camino directo se elimina en C3",
            flush=True,
        )

    if args.aplicar:
        print("=" * 60, flush=True)
        print("C1 --aplicar artefacto (sin Gemini)", flush=True)
        print("=" * 60, flush=True)
        return comando_aplicar(args.aplicar)

    if args.batch <= 0:
        print("ERROR: --batch debe ser > 0", flush=True)
        return 1

    print("=" * 60, flush=True)
    print("Fase B -- clasificar categoria_it (Gemini)", flush=True)
    print(
        f"  proponer={args.proponer}  dry-run={args.dry_run}  "
        f"filtro={args.filtro}  limit={args.limit or 'all'}  "
        f"batch={args.batch}  incluir_ventana_cerrada="
        f"{args.incluir_ventana_cerrada}  modelo={GEMINI_FLASH}",
        flush=True,
    )
    print("=" * 60, flush=True)

    if not GEMINI_API_KEY:
        print("ERROR: GEMINI_API_KEY no encontrado", flush=True)
        return 1
    supa = init_supabase()
    if supa is None:
        return 1

    print("SELECT categoria_it IS NULL AND relevancia_ia IS NULL...",
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
            return comando_proponer(supa, args, filas)
        return 0

    if args.proponer:
        return comando_proponer(supa, args, filas)
    return camino_directo(supa, args, filas)


if __name__ == "__main__":
    sys.exit(main())
