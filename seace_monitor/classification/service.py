"""Construcción de prompts del caso de uso de clasificación."""

from __future__ import annotations

from .rules import items_cubso, items_desc, recortar


def user_prompt(lote: list[dict]) -> str:
    lines = [
        "Clasifica estos contratos. Devuelve un JSON array con un objeto "
        "{id, categoria} por cada id de entrada.",
        "",
    ]
    for index, row in enumerate(lote, 1):
        lines.append(f"{index}. id={row['id']}")
        lines.append(f"   descripcion: {recortar(row.get('descripcion'), 400)}")
        lines.append(f"   objeto: {recortar(row.get('objeto'), 80)}")
        lines.append(f"   item: {recortar(items_desc(row), 240)}")
        lines.append(f"   cubso: {recortar(items_cubso(row), 240)}")
        lines.append(f"   entidad: {recortar(row.get('entidad'), 120)}")
        lines.append(f"   area_usuaria: {recortar(row.get('nom_area_usuaria'), 160)}")
        lines.append("")
    return "\n".join(lines)


def user_prompt_p2(lote: list[dict], *, pistas: str = "") -> str:
    lines = [
        "Clasifica estos contratos. Devuelve un JSON array con un objeto "
        "{id, senal, categoria} por cada id de entrada.",
        "",
    ]
    for index, row in enumerate(lote, 1):
        lines.append(f"{index}. id={row['id']}")
        lines.append(f"   descripcion: {recortar(row.get('descripcion'), 400)}")
        lines.append(f"   objeto: {recortar(row.get('objeto'), 80)}")
        lines.append(f"   item: {recortar(items_desc(row), 240)}")
        lines.append("")
    if pistas:
        lines.append(pistas)
        lines.append("")
    return "\n".join(lines)
