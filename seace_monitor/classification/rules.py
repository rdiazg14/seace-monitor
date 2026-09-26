"""Reglas puras y parsing de la clasificaci?n Gemini."""

from __future__ import annotations

import json
import unicodedata
from datetime import datetime, timezone

from .contracts import CATEGORIA_NINGUNA, CONF_ENUM, ENUM_CATEGORIA

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
            names.append(_cortar_en_palabra(_texto_colapsado(n), 80))
    return "; ".join(names)


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
            # Mismo criterio que recortar(): el n[:80] a mitad de palabra
            # era la senal que copiaba el modelo (31971 "servidor de
            # redunda", 18971 "Cableado Estructurad").
            names.append(_cortar_en_palabra(_texto_colapsado(n), 80))
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
            names.append(_cortar_en_palabra(_texto_colapsado(n), 80))
    return "; ".join(names)


def _texto_colapsado(s) -> str:
    return " ".join(str(s or "").split())


def _cortar_en_palabra(t: str, n: int) -> str:
    """Corta t (ya colapsado) a n chars. Si el corte cae a mitad de
    palabra, retrocede al ultimo espacio; si no hay espacio, corta
    como hoy (t[:n-1]). Lo usa recortar() y el recorte de 80 por item."""
    if len(t) <= n:
        return t
    limite = n - 1
    frag = t[:limite]
    if limite < len(t) and not t[limite].isspace():
        sp = frag.rfind(" ")
        if sp != -1:
            frag = frag[:sp]
    return frag


def recortar(s, n: int = 220) -> str:
    t = _texto_colapsado(s)
    if len(t) <= n:
        return t
    # El modelo copia la senal del texto recortado; un corte a mitad de
    # palabra genera senales que la verificacion rechaza (31971 "servidor
    # de redunda", 18971 "Cableado Estructurad").
    return _cortar_en_palabra(t, n) + "..."


def _bruto_mas_largo_que(bruto, n: int) -> bool:
    return len(" ".join(str(bruto or "").split())) > n


def _match_senal(ns: str, texto_recortado: str, *, truncado: bool) -> bool:
    """True si ns esta en el texto y no pega contra un recorte (ultimos 5)."""
    nt = normalizar(texto_recortado)
    idx = nt.find(ns)
    if idx < 0:
        return False
    if truncado and (idx + len(ns)) > len(nt) - 5:
        return False
    return True


def _match_item_o_cubso(ns: str, row: dict, key: str, joined: str) -> bool:
    rec = recortar(joined, 240)
    if ns not in normalizar(rec):
        return False
    items = row.get("items_json") or []
    if isinstance(items, list):
        any_hit = False
        valid = False
        for it in items[:8]:
            if not isinstance(it, dict):
                continue
            raw = (it.get(key) or "").strip()
            piece = _cortar_en_palabra(_texto_colapsado(raw), 80)
            nt = normalizar(piece)
            idx = nt.find(ns)
            if idx < 0:
                continue
            any_hit = True
            if _bruto_mas_largo_que(raw, 80) and (idx + len(ns)) > len(nt) - 5:
                continue
            valid = True
            break
        if any_hit and not valid:
            return False
    return _match_senal(
        ns, rec, truncado=_bruto_mas_largo_que(joined, 240),
    )


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
    desc = row.get("descripcion")
    obj = row.get("objeto")
    item_j = items_desc(row)
    cubso_j = items_cubso(row)
    if _match_senal(
        ns, recortar(desc, 400), truncado=_bruto_mas_largo_que(desc, 400),
    ):
        return True, "descripcion"
    if _match_senal(
        ns, recortar(obj, 80), truncado=_bruto_mas_largo_que(obj, 80),
    ):
        return True, "objeto"
    if _match_item_o_cubso(ns, row, "descripcion", item_j):
        return True, "item"
    if _match_item_o_cubso(ns, row, "nom_cubso", cubso_j):
        return True, "cubso"
    return False, "ninguna"


def verificar_senal_p2(senal: str, row: dict) -> tuple[bool, str]:
    """Igual que verificar_senal pero sin cubso: P2 no vio ese campo."""
    ns = normalizar(senal)
    if len(ns) < 4:
        return False, "ninguna"
    desc = row.get("descripcion")
    obj = row.get("objeto")
    item_j = items_desc(row)
    if _match_senal(
        ns, recortar(desc, 400), truncado=_bruto_mas_largo_que(desc, 400),
    ):
        return True, "descripcion"
    if _match_senal(
        ns, recortar(obj, 80), truncado=_bruto_mas_largo_que(obj, 80),
    ):
        return True, "objeto"
    if _match_item_o_cubso(ns, row, "descripcion", item_j):
        return True, "item"
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
