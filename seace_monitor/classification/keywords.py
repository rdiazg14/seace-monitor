"""Carga de la cascada configurable de palabras clave."""

from __future__ import annotations


def cargar_keywords(supa) -> list[tuple[str, list[dict]]] | None:
    """Devuelve categorías y reglas activas en orden de prioridad."""
    if not supa:
        return None
    try:
        response = (
            supa.table("it_keywords")
            .select("id,categoria,keyword,tipo,limite_palabra,prioridad,tolera_plural")
            .eq("activa", True)
            .order("prioridad")
            .order("id")
            .limit(5000)
            .execute()
        )
    except Exception as error:
        print(f"[keywords] SELECT it_keywords fallo: {error}", flush=True)
        return None
    rows = response.data or []
    if not rows:
        return None
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["categoria"], []).append({
            "keyword": row["keyword"],
            "tipo": row.get("tipo") or "incluye",
            "limite_palabra": bool(row.get("limite_palabra")),
            "tolera_plural": bool(row.get("tolera_plural")),
        })
    return list(groups.items())
