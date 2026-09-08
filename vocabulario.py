#!/usr/bin/env python3
"""Aprendizaje autonomo de vocabulario (ARQUITECTURA_DATOS §11).

Registro de senales Gemini en keyword_candidatas. Evaluacion tipo A/B
vive en scripts/evaluar_candidatas.py. Este modulo no activa keywords.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

from ingesta_completa import _norm

CATEGORIA_NINGUNA = "ninguna"

UMBRAL_AUTO = 50
MIN_VECES = 3
RATIO_PREDICTIVO_MIN = 0.30
MAX_PISTAS = 20
MAX_PISTAS_CHARS = 800
MIN_CHARS_TERMINO = 4
MAX_CHARS_TERMINO = 40
MAX_PALABRAS_TERMINO = 4

# Mas largo primero. 1.1 + colas de proceso que no son el nucleo.
_PREFIJOS_LICITACION = (
    "suscripcion a",
    "suscripcion de",
    "mantenimiento de",
    "implementacion de",
    "elaboracion de",
    "contratacion de",
    "adquisicion de",
    "suministro de",
    "alquiler de",
    "renovacion de",
    "provision de",
    "formulacion del",
    "formulacion de",
    "mantenimiento correctivo",
    "mantenimiento preventivo",
    "licencias de",
    "licencia de",
    "analisis de",
    "compra de",
    "servicios de",
    "servicio de",
    "renovacion del",
    "contratacion",
    "adquisicion",
    "licencias",
    "licencia",
    "servicios",
    "servicio",
    "suscripcion",
    "renovacion",
    "mantenimiento",
    "para el",
    "para la",
    "de la",
    "del",
    "para",
)

_VACIAS_INICIO = frozenset(
    p.split()[0] for p in _PREFIJOS_LICITACION
)
_VACIAS_CORTE = frozenset(
    p.split()[0] for p in _PREFIJOS_LICITACION if len(p.split()[0]) >= 6
)

_ARTICULOS = frozenset({"el", "la", "los", "las", "un", "una", "unos", "unas"})
_STOP_BORDE = frozenset({
    "de", "del", "la", "el", "los", "las", "y", "a", "para", "en", "con",
    "un", "una", "por", "al",
})
_TEMPORALES = (
    "anual en linea a",
    "anual en linea",
    "anual a",
    "anual de",
    "anual",
    "mensual de",
    "mensual",
    "en linea a",
    "en linea",
    "a traves de",
)
_COLA_ADMIN = (
    "para el area",
    "para la area",
    "para la unidad",
    "para el area de",
    "de la municipalidad",
    "municipalidad",
    "ministerio de",
    "gobierno regional",
    "ugel",
)
_GENERIC_LEAD = frozenset({
    "comunicacion", "solucion", "componentes", "infraestructura",
})
_SPEC_TRAIL = frozenset({
    "pci", "pcie", "sata", "hdmi", "ddr", "ddr4", "ddr5", "ghz", "mhz",
    "ssl", "tls", "almacenamiento", "pantalla", "pulgada", "pulgadas",
})
_TRAIL_ADJ = frozenset({
    "redundante", "redundantes", "portable", "portables",
    "integral", "integrales", "complementario", "complementaria",
    "avanzado", "avanzada", "avanzados", "corporativo", "corporativa",
    "especializado", "especializada", "gestionada", "gestionado",
})
_NO_SING = frozenset({
    "datos", "windows", "linux", "ios", "nas", "plus", "gas", "redes",
    "analisis", "sms", "ssl", "tls", "mdm", "edr", "xdr", "usb",
    "docs", "gis", "pad", "saas", "paas", "iaas", "traves",
    "arcgis", "autocad", "autodesk",
})
_GENERIC_SOLOS = frozenset({
    "software", "sistema", "plataforma", "desarrollo", "gestion",
    "soporte", "instalacion", "infraestructura", "componentes",
    "componente", "actualizacion", "automatizacion", "implementacion",
    "fortalecimiento", "configuracion", "administracion", "solucion",
    "equipo", "equipos", "servicio", "servicios", "licencia", "licencias",
    "disco", "red", "dato", "datos",
})
_CORTOS_OK = frozenset({
    "sms", "nas", "usb", "edr", "xdr", "mdm", "sap", "erp", "pdf",
    "sql", "gis", "iot", "vpn", "ssl", "tls", "pad", "3d", "ip",
    "web", "app", "cad", "pro", "bios", "cpu", "gpu", "ram",
})
_UNIDADES = re.compile(
    r"\b\d+([.,]\d+)?\s*(gb|mb|tb|ghz|mhz|kg|mm|cm|mbps|kbps|khz|folios?)?\b"
)
_MIN_KW_NUCLEO = 6


def normalizar(s: str) -> str:
    t = unicodedata.normalize("NFKD", str(s or ""))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.lower()
    t = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in t)
    return " ".join(t.split()).strip()


def n_palabras(s: str) -> int:
    return len((s or "").split())


def empieza_con_vacia_licitacion(s: str) -> bool:
    w = (s or "").split()[:1]
    return bool(w) and w[0] in _VACIAS_INICIO


def _strip_lista(t: str, prefijos: tuple[str, ...]) -> str:
    changed = True
    while t and changed:
        changed = False
        for p in prefijos:
            if t == p:
                return ""
            if t.startswith(p + " "):
                t = t[len(p):].strip()
                changed = True
                break
    return t


def _strip_borde(words: list[str]) -> list[str]:
    while words and words[0] in _STOP_BORDE | _ARTICULOS:
        words = words[1:]
    while words and words[-1] in _STOP_BORDE:
        words = words[:-1]
    return words


def _singular(w: str) -> str:
    if w in _NO_SING or len(w) <= 3:
        return w
    if w.endswith("is"):
        return w
    if w.endswith("ores") and len(w) > 5:
        return w[:-2]
    # digitales→digital; el resto de -es se trata como plural en -s
    if w.endswith(("les", "nes", "res")) and len(w) > 5:
        return w[:-2]
    if w.endswith("es"):
        return w[:-1]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def _singularizar(words: list[str]) -> list[str]:
    return [_singular(w) for w in words]


def _nucleo_keyword(texto: str, categoria: str, keywords: list[dict] | None) -> str | None:
    """Keyword incluye activa (misma cat) contenida como secuencia. La mas larga."""
    if not texto or not keywords:
        return None
    best = ""
    for kw in keywords:
        if kw.get("categoria") != categoria:
            continue
        if (kw.get("tipo") or "incluye") != "incluye":
            continue
        if not kw.get("activa", True):
            continue
        k = normalizar(kw.get("keyword") or "")
        if len(k) < _MIN_KW_NUCLEO:
            continue
        if k in _VACIAS_INICIO or k in _GENERIC_SOLOS:
            continue
        if re.search(r"\b" + re.escape(k) + r"\b", texto):
            if len(k) > len(best):
                best = k
    return best or None


def extraer_termino(
    senal: str,
    *,
    categoria: str = "",
    keywords: list[dict] | None = None,
) -> str | None:
    """Nucleo tecnico de una senal Gemini. None si no es extraible."""
    t = normalizar(senal)
    if not t:
        return None
    t = _strip_lista(t, _PREFIJOS_LICITACION)
    t = _strip_lista(t, _TEMPORALES)
    t = _strip_lista(t, _PREFIJOS_LICITACION)
    words0 = _strip_borde(t.split())
    t = _strip_lista(" ".join(words0), _PREFIJOS_LICITACION)
    t = _strip_lista(t, _TEMPORALES)
    for marca in _COLA_ADMIN:
        i = t.find(marca)
        if i > 0:
            t = t[:i].strip()
    i_traves = t.find(" a traves ")
    if i_traves > 0 and n_palabras(t[:i_traves]) >= 2:
        t = t[:i_traves].strip()
    i_para = t.find(" para ")
    if i_para > 0:
        izq = t[:i_para].strip()
        der = t[i_para + 6:].strip()
        if izq in _GENERIC_SOLOS or (n_palabras(izq) == 1 and izq in _VACIAS_INICIO):
            t = der
        elif n_palabras(izq) >= 2 or (n_palabras(izq) == 1 and len(izq) >= 6):
            t = izq
    t = _UNIDADES.sub(" ", t)
    t = " ".join(t.split())
    if " y " in t:
        izq, _der = t.split(" y ", 1)
        if n_palabras(izq) >= 2:
            t = izq.strip()
    words = t.split()
    while words and words[-1] in _SPEC_TRAIL:
        words.pop()
    while words and words[-1] in _TRAIL_ADJ:
        words.pop()
    words = _strip_borde(words)
    if words and words[0] in _GENERIC_LEAD and len(words) >= 3:
        words = _strip_borde(words[1:])
    if "nas" in words and "network" in words:
        idx = words.index("network")
        words = words[idx:idx + 2]
    for i, w in enumerate(words):
        if i > 0 and w in _VACIAS_CORTE:
            resto = words[i + 1:]
            if resto and 1 <= len(resto) <= MAX_PALABRAS_TERMINO + 1:
                words = resto
            break
    if len(words) > MAX_PALABRAS_TERMINO:
        first = words[:MAX_PALABRAS_TERMINO]
        last = words[-MAX_PALABRAS_TERMINO:]
        if first[-1] in _STOP_BORDE and last[-1] not in _STOP_BORDE:
            words = last
        elif first[-1] in _STOP_BORDE and last[-1] in _STOP_BORDE:
            words = _strip_borde(words[- (MAX_PALABRAS_TERMINO + 1):])
            words = words[-MAX_PALABRAS_TERMINO:]
        else:
            words = first
    words = _strip_borde(words)
    while words and len(words[-1]) <= 3 and words[-1] not in _CORTOS_OK and not any(
        ch.isdigit() for ch in words[-1]
    ):
        words.pop()
    if words and len(words[0]) <= 3 and len(words) >= 3 and len(words[1]) >= 6:
        words = words[1:]
    words = _singularizar(words)
    words = _strip_borde(words)
    t = _strip_lista(" ".join(words), _PREFIJOS_LICITACION)
    t = _strip_lista(t, _TEMPORALES)
    words = _strip_borde(t.split())
    t = " ".join(words).strip()
    if not t:
        return None
    nucleo = _nucleo_keyword(t, categoria, keywords) or _nucleo_keyword(
        normalizar(senal), categoria, keywords,
    )
    if nucleo and len(nucleo) < len(t):
        t = nucleo
    if not (MIN_CHARS_TERMINO <= len(t) <= MAX_CHARS_TERMINO):
        return None
    if not (1 <= n_palabras(t) <= MAX_PALABRAS_TERMINO):
        return None
    if empieza_con_vacia_licitacion(t):
        return None
    if t in _GENERIC_SOLOS:
        return None
    if n_palabras(t) == 1 and len(t) <= 6 and t not in _CORTOS_OK:
        return None
    return t


def termino_valido_activar(termino: str) -> bool:
    if not termino:
        return False
    if n_palabras(termino) > MAX_PALABRAS_TERMINO:
        return False
    if empieza_con_vacia_licitacion(termino):
        return False
    if not (MIN_CHARS_TERMINO <= len(termino) <= MAX_CHARS_TERMINO):
        return False
    return True


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            ins = cur[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[-1]


def es_tipo_a(senal: str, keyword: str) -> tuple[bool, dict]:
    """Guardas §11: Levenshtein min>=6 o prefijo/sufijo corto>=5 y >=60% madre."""
    s = _norm(senal).strip()
    m = _norm(keyword).strip()
    if not s or not m or s == m:
        return False, {}
    ev = {"senal": s, "madre": m}
    nmin = min(len(s), len(m))
    dist = levenshtein(s, m)
    ev["levenshtein"] = dist
    if dist <= 2 and nmin >= 6:
        ev["regla"] = "levenshtein"
        return True, ev
    # La senal es prefijo o sufijo de la madre (desarro/desarrollo), no al reves.
    if nmin >= 5 and nmin * 10 >= len(m) * 6:
        if m.startswith(s) or m.endswith(s):
            ev["regla"] = "prefijo_sufijo"
            ev["ratio_madre"] = round(nmin / len(m), 3)
            return True, ev
    return False, ev


def senal_es_keyword_activa(
    senal: str,
    categoria: str,
    keywords_incluye: list[dict],
) -> bool:
    ns = _norm(senal).strip()
    if not ns:
        return True
    for kw in keywords_incluye:
        if kw.get("categoria") != categoria:
            continue
        if (kw.get("tipo") or "incluye") != "incluye":
            continue
        if not kw.get("activa", True):
            continue
        if _norm(kw.get("keyword") or "").strip() == ns:
            return True
    return False


def madre_tipo_a(senal: str, categoria: str, kws: list[dict]) -> tuple[dict | None, dict]:
    best = None
    best_ev: dict = {}
    for kw in kws:
        if kw.get("categoria") != categoria:
            continue
        ok, ev = es_tipo_a(senal, kw.get("keyword") or "")
        if not ok:
            continue
        score = (ev.get("levenshtein", 99), len(kw.get("keyword") or ""))
        if best is None or score < best[0]:
            best = (score, kw)
            best_ev = ev
    return (best[1] if best else None), best_ev


def clasificar_cobertura(
    termino: str,
    categoria: str,
    kws: list[dict],
) -> tuple[str, dict | None, dict]:
    """('exact'|'tipo_a'|'libre', madre|None, evidencia)."""
    if not termino:
        return "libre", None, {}
    if senal_es_keyword_activa(termino, categoria, kws):
        madre = None
        for kw in kws:
            if kw.get("categoria") != categoria:
                continue
            if _norm(kw.get("keyword") or "").strip() == _norm(termino).strip():
                madre = kw
                break
        return "exact", madre, {}
    madre, ev = madre_tipo_a(termino, categoria, kws)
    if madre:
        return "tipo_a", madre, ev
    return "libre", None, {}


def keywords_incluye_activas(supa) -> list[dict]:
    rows = (
        supa.table("it_keywords")
        .select("id,categoria,keyword,tipo,prioridad,limite_palabra,tolera_plural,activa")
        .eq("activa", True)
        .eq("tipo", "incluye")
        .limit(5000)
        .execute()
        .data
        or []
    )
    return rows


def registrar_candidata(
    supa,
    *,
    senal: str,
    categoria: str,
    contrato_id: int,
    keywords: list[dict] | None = None,
) -> str:
    """Upsert keyword_candidatas. No activa. Retorna skip|nueva|inc."""
    kws = keywords if keywords is not None else keywords_incluye_activas(supa)
    termino = extraer_termino(senal, categoria=categoria, keywords=kws)
    if not termino or categoria == CATEGORIA_NINGUNA:
        return "skip"
    cob, _madre, _ev = clasificar_cobertura(termino, categoria, kws)
    if cob in ("exact", "tipo_a"):
        return "skip"
    ns = termino
    prev = (
        supa.table("keyword_candidatas")
        .select("id,estado,veces_vista,contratos")
        .eq("senal", ns)
        .eq("categoria_propuesta", categoria)
        .limit(1)
        .execute()
        .data
    )
    now = datetime.now(timezone.utc).isoformat()
    if prev:
        row = prev[0]
        if row.get("estado") == "rechazada":
            return "skip"
        if row.get("estado") in ("auto_activada", "aprobada_admin"):
            return "skip"
        ids = list(row.get("contratos") or [])
        cid = int(contrato_id)
        if cid not in ids:
            ids.append(cid)
        (
            supa.table("keyword_candidatas")
            .update({
                "veces_vista": int(row.get("veces_vista") or 0) + 1,
                "contratos": ids,
                "ultima_vez_utc": now,
            })
            .eq("id", row["id"])
            .execute()
        )
        return "inc"
    (
        supa.table("keyword_candidatas")
        .insert({
            "senal": ns,
            "categoria_propuesta": categoria,
            "veces_vista": 1,
            "contratos": [int(contrato_id)],
            "ejemplo_contrato_id": int(contrato_id),
            "primera_vez_utc": now,
            "ultima_vez_utc": now,
            "estado": "nueva",
        })
        .execute()
    )
    return "nueva"


def extraer_senal_item(it: dict) -> tuple[str | None, str | None]:
    """(senal, categoria) si verificada y != ninguna."""
    p2 = it.get("p2") if isinstance(it.get("p2"), dict) else None
    p1 = it.get("p1") if isinstance(it.get("p1"), dict) else None
    src = None
    if p2 and p2.get("categoria") and p2.get("categoria") != CATEGORIA_NINGUNA:
        if p2.get("senal_verificada"):
            src = p2
    if src is None and p1 and p1.get("categoria") != CATEGORIA_NINGUNA:
        if p1.get("senal_verificada"):
            src = p1
    if src is None:
        return None, None
    senal = (src.get("senal") or "").strip()
    cat = src.get("categoria")
    if not senal or not cat or cat == CATEGORIA_NINGUNA:
        return None, None
    return senal, cat


def registrar_desde_items(supa, items: list[dict]) -> tuple[int, int]:
    kws = keywords_incluye_activas(supa)
    nuevas = 0
    incs = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        senal, cat = extraer_senal_item(it)
        if not senal or not cat:
            continue
        try:
            cid = int(it["id"])
        except (TypeError, ValueError, KeyError):
            continue
        r = registrar_candidata(
            supa, senal=senal, categoria=cat, contrato_id=cid, keywords=kws,
        )
        if r == "nueva":
            nuevas += 1
        elif r == "inc":
            incs += 1
    return nuevas, incs


def cargar_pistas(supa) -> str:
    """Bloque para user_prompt_p2. Vacio si no hay candidatas utiles."""
    try:
        medidas = (
            supa.table("keyword_candidatas")
            .select("senal,categoria_propuesta,veces_vista,estado")
            .eq("estado", "medida")
            .order("veces_vista", desc=True)
            .limit(40)
            .execute()
            .data
            or []
        )
        nuevas = (
            supa.table("keyword_candidatas")
            .select("senal,categoria_propuesta,veces_vista,estado")
            .eq("estado", "nueva")
            .gte("veces_vista", 2)
            .order("veces_vista", desc=True)
            .limit(40)
            .execute()
            .data
            or []
        )
    except Exception as e:
        print(f"[vocabulario] no se cargaron pistas: {e}", flush=True)
        return ""
    filas = list(medidas) + list(nuevas)
    lineas = [
        "Vocabulario observado (pista; no es regla):",
    ]
    n = 0
    chars = 0
    for r in filas:
        senal = (r.get("senal") or "").strip()
        cat = (r.get("categoria_propuesta") or "").strip()
        if not senal or not cat:
            continue
        ln = f"{senal} -> {cat} (pista; no es regla)"
        if n >= MAX_PISTAS or chars + len(ln) + 1 > MAX_PISTAS_CHARS:
            break
        lineas.append(ln)
        n += 1
        chars += len(ln) + 1
    if n == 0:
        return ""
    return "\n".join(lineas)
