#!/usr/bin/env python3
"""Aprendizaje autonomo de vocabulario (ARQUITECTURA_DATOS §11).

Registro de senales Gemini en keyword_candidatas. Evaluacion tipo A/B
vive en scripts/evaluar_candidatas.py. Este modulo no activa keywords.
"""
from __future__ import annotations

import unicodedata
from datetime import datetime, timezone

from ingesta_completa import _norm

CATEGORIA_NINGUNA = "ninguna"


def normalizar(s: str) -> str:
    t = unicodedata.normalize("NFKD", str(s or ""))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.lower()
    t = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in t)
    return " ".join(t.split()).strip()

UMBRAL_AUTO = 50
MIN_VECES = 3
MAX_PISTAS = 20
MAX_PISTAS_CHARS = 800


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
    ns = normalizar(senal)
    if not ns or categoria == CATEGORIA_NINGUNA:
        return "skip"
    kws = keywords if keywords is not None else keywords_incluye_activas(supa)
    if senal_es_keyword_activa(ns, categoria, kws):
        return "skip"
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
