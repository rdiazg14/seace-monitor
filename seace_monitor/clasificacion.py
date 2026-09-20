"""Clasificación IT/IA pura (sin BD, sin red).

Extrae de ``ingesta_completa.py`` las reglas determinísticas de clasificación:
normalización NFKD, prioridad de ``IT_CATS``, relevancia IA y el matcher de
keywords de tabla. ``ingesta_completa.py`` re-exporta estos nombres para no
romper a ``vocabulario.py`` / ``reclasificar_categoria.py`` (expand-contract).
"""
from __future__ import annotations

import re
import unicodedata

# Primera coincidencia en la lista gana (orden = prioridad).
IT_CATS: list[tuple[str, list[str]]] = [
    ("Firma digital", [
        "firma digital", "certificado digital", "certificado electronico",
        "token criptografico",
    ]),
    ("IA/analytics", [
        "inteligencia artificial", "machine learning", "ia generativa",
        "chatbot", "asistente virtual", "llm", "gpt", "copilot",
        "gemini", "claude", "openai", "azure openai",
        "analytics", "business intelligence", "ciencia de datos", "big data",
        "procesamiento de lenguaje", "red neuronal", "deep learning",
        "tokens de procesamiento",
    ]),
    ("Ciberseguridad", [
        "ciberseguridad", "seguridad informatica", "seguridad de la informacion",
        "firewall", "pentest", "ethical hacking",
    ]),
    ("Cloud/hosting", [
        "nube publica", "cloud computing", "hosting", "servidor virtual",
        " aws ", "google cloud",
    ]),
    ("Microsoft", [
        "microsoft", "office 365", "microsoft 365",
        "sharepoint", "exchange", "windows server",
    ]),
    ("Oracle", ["oracle database", "oracle ebs", "peoplesoft"]),
    ("Base de datos/ERP", [
        "base de datos", "sql server", "postgresql", "mysql", "mongodb",
        "data warehouse", " sap ", " erp ",
    ]),
    ("Desarrollo software", [
        "desarrollo de software", "desarrollo de sistema",
        "sistema de informacion", "aplicativo", "software a medida",
        "plataforma web", "portal web", "sistema web",
        "sistema administrativo", "aplicacion movil", "app movil",
        "implementacion de software",
    ]),
    ("Licencias", [
        "licencia de software", "licenciamiento", "suscripcion de software",
    ]),
    ("Soporte tecnico", [
        "soporte tecnico", "mantenimiento de software",
        "mantenimiento de sistema", "mesa de ayuda", "helpdesk", "help desk",
    ]),
    ("Redes/cableado", [
        "red de datos", "cableado estructurado", " switch ", "router",
        "fibra optica", " wifi", "wireless", "access point", "punto de acceso",
    ]),
    ("Correo electronico", [
        "correo electronico", "mensajeria electronica",
    ]),
    ("Hardware", [
        "computadora", "laptop", "impresora", " monitor ", "disco duro",
        "memoria ram", " ups ", "proyector", " tablet ",
        "equipos informaticos", "equipos de computo", "scanner", "escaner",
    ]),
]

# Relevancia IA
KW_ALTA = [
    "token", "azure openai", "openai", "gpt", "llm",
    "claude", "copilot", "gemini",
]
KW_GENERICOS = [
    "inteligencia artificial", "ia generativa", "chatbot", "asistente virtual",
    "machine learning", "aprendizaje automatico", "procesamiento de lenguaje",
    "vision computacional", "deep learning", "red neuronal",
    "modelo de lenguaje", "ciencia de datos", "big data",
]
_KW_LIMITE_PALABRA: set[str] = {"ia"}


def _norm(texto: str) -> str:
    """Minúsculas y sin tildes."""
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", str(texto))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.lower()


def _contiene(texto_norm: str, kw: str, limite_palabra: bool | None = None) -> bool:
    kn = _norm(kw)
    if limite_palabra is None:
        limite_palabra = kn in _KW_LIMITE_PALABRA
    if limite_palabra:
        return bool(re.search(r"\b" + re.escape(kn) + r"\b", texto_norm))
    return kn in texto_norm


def _texto_contrato(r: dict) -> str:
    """Concatena campos de texto de un registro API para clasificación."""
    return " " + " ".join(
        _norm(str(r.get(k, "")))
        for k in ("desObjetoContrato", "desContratacion",
                  "nomObjetoContrato", "nomEntidad")
    ) + " "


def _match_kw_tabla(texto_norm: str, d: dict) -> bool:
    """Misma regla que backfill_categoria: tolera_plural o substring/\\b."""
    kw = d["keyword"]
    if d.get("tolera_plural"):
        kn = _norm(kw)
        words = kn.split()
        if not words:
            return False
        parts = [re.escape(w) + r"e?s?" for w in words]
        return bool(re.search(r"\b" + r"\s+".join(parts) + r"\b", texto_norm))
    return _contiene(texto_norm, kw, bool(d.get("limite_palabra")))


def clasificar_categoria_it(
    r: dict,
    cats: list[tuple[str, list[dict]]] | None = None,
) -> str | None:
    """Primera categoria por prioridad. cats=None usa IT_CATS (fallback).

    tipo 'excluye': si matchea, esa categoria no gana y la cascada sigue.
    tipo 'incluye': si matchea, gana. limite_palabra True = \\b...\\b.
    tolera_plural True = s/es opcional por palabra (solo keywords de tabla).
    """
    t = _texto_contrato(r)
    if cats is None:
        for cat, kws in IT_CATS:
            if any(_contiene(t, kw) for kw in kws):
                return cat
        return None
    for cat, kws in cats:
        if any(
            _match_kw_tabla(t, d)
            for d in kws if d.get("tipo") == "excluye"
        ):
            continue
        if any(
            _match_kw_tabla(t, d)
            for d in kws if d.get("tipo") != "excluye"
        ):
            return cat
    return None


def clasificar_relevancia_ia(r: dict) -> str | None:
    t = _texto_contrato(r)
    if any(_contiene(t, kw) for kw in KW_ALTA):
        return "ALTA"
    gen = [kw for kw in KW_GENERICOS if _contiene(t, kw)]
    if len(gen) >= 2:
        return "MEDIA"
    if len(gen) == 1:
        return "BAJA"
    return None
