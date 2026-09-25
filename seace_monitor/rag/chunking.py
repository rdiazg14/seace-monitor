"""Transformaciones puras para construir chunks de ficha y TDR.

Este módulo no lee configuración, no accede a Supabase y no produce efectos
externos. Los entrypoints conservan la selección, persistencia y CLI.
"""

from __future__ import annotations

import json
import re


MAX_TOKENS_ANTES_SPLIT = 800
TARGET_SUBCHUNK = 500

_STOP_SIGLAS = frozenset({
    "DE", "DEL", "Y", "E", "DA", "DO", "DAS", "AL", "A",
})
_HEADER_LINE_RE = re.compile(r"^\[[^\]]+\]\s*(?:\n|$)")
_MEMBRETE_LINE_RE = re.compile(
    r"(?i)^("
    r"---\s*pagina\s+\d+\s*---"
    r"|per[uú]"
    r"|ministerio de defensa"
    r"|av\.\s*del parque norte\b.*"
    r"|https?://\S+"
    r"|www\.gob\.pe/\S*"
    r"|facilita\.gob\.pe\S*"
    r"|mesadepartes@\S+"
    r"|[“\"']decenio de la igualdad.*"
    r"|[“\"']año de la esperanza.*"
    r"|centro nacional de estimaci[oó]n,?"
    r"|centro nacional de estimaci[oó]n,?\s*prevenci[oó]n y reducci[oó]n.*"
    r"|prevenci[oó]n y reducci[oó]n del"
    r"|riesgo de desastres\s*[-–]\s*cenepred"
    r"|cenepred"
    r"|subdirecci[oó]n de"
    r"|gesti[oó]n de la"
    r"|informaci[oó]n"
    r")$"
)


def approx_tokens(texto: str) -> int:
    """Estimación barata (~4 chars/token). Evita dependencia de tiktoken."""
    if not texto:
        return 0
    return max(1, len(texto) // 4)


def split_por_parrafos(texto: str, target_tokens: int) -> list[str]:
    partes = [p.strip() for p in texto.replace("\r\n", "\n").split("\n") if p.strip()]
    if not partes:
        return [texto.strip()] if texto.strip() else []

    chunks: list[str] = []
    buf: list[str] = []
    buf_tok = 0
    for parte in partes:
        parte_tokens = approx_tokens(parte)
        if buf and buf_tok + parte_tokens > target_tokens:
            chunks.append("\n".join(buf))
            buf, buf_tok = [parte], parte_tokens
        else:
            buf.append(parte)
            buf_tok += parte_tokens
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def nro_contrato(contrato: dict) -> str:
    return (
        (contrato.get("descripcion_contrato") or "").strip()
        or str(contrato.get("nro_contratacion") or "")
        or str(contrato.get("id") or "")
    )


def siglas_entidad(entidad: str) -> str:
    """Iniciales del tramo más específico (después del último guion)."""
    raw = (entidad or "").strip()
    if not raw:
        return "s/e"
    partes = [p.strip() for p in re.split(r"\s*[-–—/]\s*", raw) if p.strip()]
    foco = partes[-1] if partes else raw
    iniciales: list[str] = []
    for palabra in re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9]+", foco):
        upper = palabra.upper()
        if upper in _STOP_SIGLAS:
            continue
        iniciales.append(upper[0])
    siglas = "".join(iniciales)
    if len(siglas) < 2:
        siglas = re.sub(r"[^A-Za-z0-9]", "", foco)[:12].upper() or "s/e"
    return siglas[:16]


def encabezado(contrato: dict) -> str:
    """Header largo de chunks fuente=api."""
    entidad = (contrato.get("entidad") or "").strip() or "s/e"
    asunto = (contrato.get("descripcion") or contrato.get("objeto") or "").strip()
    asunto = " ".join(asunto.split())[:80] or "s/a"
    return f"[{entidad} | {asunto} | {nro_contrato(contrato)}]"


def encabezado_pdf(contrato: dict) -> str:
    """Header corto de display para chunks PDF."""
    return f"[{siglas_entidad(contrato.get('entidad') or '')} | {nro_contrato(contrato)}]"


def cuerpo_chunk(texto: str) -> str:
    """Devuelve el cuerpo sin la primera línea de header entre corchetes."""
    valor = texto or ""
    match = _HEADER_LINE_RE.match(valor)
    return valor[match.end():].lstrip() if match else valor


def _es_siglas_membrete(texto: str) -> bool:
    """Detecta una línea corta en mayúsculas usada como membrete."""
    if not texto or len(texto) > 40 or re.match(r"^\d+", texto):
        return False
    palabras = texto.split()
    if not (1 <= len(palabras) <= 4):
        return False
    if not any(ch.isalpha() for ch in texto) or any(ch.islower() for ch in texto):
        return False
    return not texto.endswith(":")


def _colapsar_vacias(lineas: list[str]) -> list[str]:
    salida: list[str] = []
    vacia = False
    for linea in lineas:
        if not linea.strip():
            if not vacia:
                salida.append("")
            vacia = True
        else:
            salida.append(linea)
            vacia = False
    return salida


def cuerpo_sin_membrete(texto: str) -> str:
    """Quita membretes del extractor PDF sin modificar el texto de display."""
    salida: list[str] = []
    for linea in (texto or "").splitlines():
        limpia = linea.strip()
        if limpia and (_MEMBRETE_LINE_RE.match(limpia) or _es_siglas_membrete(limpia)):
            continue
        salida.append(linea)
    return "\n".join(_colapsar_vacias(salida)).strip()


def objeto_corto(contrato: dict) -> str:
    asunto = (contrato.get("descripcion") or contrato.get("objeto") or "").strip()
    return " ".join(asunto.split())[:80] or "s/a"


def embed_text_pdf(contrato: dict, texto_display: str) -> str:
    """Construye header semántico más cuerpo limpio para embedding PDF."""
    cuerpo = cuerpo_sin_membrete(cuerpo_chunk(texto_display))
    entidad = (contrato.get("entidad") or "").strip() or "s/e"
    return f"[{entidad} | {objeto_corto(contrato)} | {nro_contrato(contrato)}] {cuerpo}".strip()


def con_contexto(contrato: dict, texto: str) -> str:
    return f"{encabezado(contrato)}\n{texto}"


def con_contexto_pdf(contrato: dict, texto: str) -> str:
    return f"{encabezado_pdf(contrato)}\n{texto}"


def meta_de_contrato(contrato: dict) -> dict:
    numero = nro_contrato(contrato)
    return {
        "meta_entidad": (contrato.get("entidad") or "").strip() or None,
        "meta_nro": numero or None,
    }


def chunks_de_pdf(contrato: dict, chunk_index_offset: int = 0) -> list[dict]:
    """Construye chunks del TDR extraído sin persistirlos."""
    tdr = (contrato.get("tdr_texto") or "").strip()
    if not tdr:
        return []
    partes = (
        split_por_parrafos(tdr, TARGET_SUBCHUNK)
        if approx_tokens(tdr) > MAX_TOKENS_ANTES_SPLIT
        else [tdr]
    )
    meta = meta_de_contrato(contrato)
    salida: list[dict] = []
    for indice, parte in enumerate(partes):
        fila = {
            "contrato_id": contrato["id"],
            "chunk_index": chunk_index_offset + indice,
            "tipo": "TDR PDF" if len(partes) == 1 else f"TDR PDF ({indice + 1}/{len(partes)})",
            "texto": con_contexto_pdf(contrato, parte),
            "fuente": "pdf",
        }
        fila["chunk_embed_text"] = embed_text_pdf(contrato, fila["texto"])
        fila.update(meta)
        salida.append(fila)
    return salida


def chunks_de_contrato(contrato: dict) -> list[dict]:
    """Construye chunks de ficha e ítems sin persistirlos."""
    contrato_id = contrato["id"]
    salida: list[dict] = []
    indice = 0

    descripcion = (contrato.get("descripcion") or "").strip()
    if descripcion:
        partes = (
            split_por_parrafos(descripcion, TARGET_SUBCHUNK)
            if approx_tokens(descripcion) > MAX_TOKENS_ANTES_SPLIT
            else [descripcion]
        )
        total = len(partes)
        for posicion, parte in enumerate(partes, 1):
            tipo = "Descripción general" if total == 1 else f"Descripción general ({posicion}/{total})"
            salida.append({
                "contrato_id": contrato_id,
                "chunk_index": indice,
                "tipo": tipo,
                "texto": con_contexto(contrato, parte),
                "fuente": "api",
            })
            indice += 1

    items = contrato.get("items_json") or []
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except json.JSONDecodeError:
            items = []
    if not isinstance(items, list):
        items = []

    for numero, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        texto = (
            f"CUBSO: {item.get('cod_cubso') or ''} - {item.get('nom_cubso') or ''}. "
            f"Cantidad: {item.get('cantidad') or ''} {item.get('unidad') or ''}. "
            f"Lugar: {item.get('distrito') or ''}. "
            f"Especificaciones: {item.get('descripcion') or ''}"
        ).strip()
        salida.append({
            "contrato_id": contrato_id,
            "chunk_index": indice,
            "tipo": f"Ítem técnico {numero}",
            "texto": con_contexto(contrato, texto),
            "fuente": "api",
        })
        indice += 1

    metadata = (
        f"Entidad: {contrato.get('entidad') or ''}. "
        f"Área usuaria: {contrato.get('nom_area_usuaria') or ''}. "
        f"Objeto: {contrato.get('objeto') or ''}. "
        f"Estado: {contrato.get('estado') or ''}. "
        f"Número: {contrato.get('nro_contratacion') or ''}."
    )
    salida.append({
        "contrato_id": contrato_id,
        "chunk_index": indice,
        "tipo": "Metadata",
        "texto": con_contexto(contrato, metadata),
        "fuente": "api",
    })
    return salida
