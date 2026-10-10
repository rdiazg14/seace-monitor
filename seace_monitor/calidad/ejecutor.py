"""Ejecuta los casos aprobados contra el asistente y captura observaciones (GW-006).

Cada caso es una consulta real de pago: la corrida se detiene antes de superar
el presupuesto acordado y lo que no se ejecutó queda como "sin observación", no
como falla del modelo. El transporte se inyecta; las pruebas no usan red.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path

from seace_monitor.calidad.evaluacion import (
    Caso,
    Corrida,
    Observacion,
    cargar_dataset,
    casos_evaluables,
    evaluar,
    registro_corrida,
)

# (pregunta) -> (status HTTP, cuerpo JSON ya decodificado o None)
Consultar = Callable[[str], tuple[int, dict | None]]


class PresupuestoAgotado(Exception):
    """El gasto acumulado alcanzó el tope antes de terminar los casos."""


def observar(caso: Caso, consultar: Consultar) -> tuple[Observacion, float, str | None]:
    """Una consulta por caso. Devuelve la observación, su costo y el modelo que respondió."""
    try:
        status, cuerpo = consultar(caso.pregunta)
    except Exception as err:  # noqa: BLE001 - un fallo de transporte es un veredicto "error", no una excepción de la corrida
        return Observacion(caso_id=caso.id, error=type(err).__name__), 0.0, None
    cuerpo = cuerpo or {}
    ids = [r["id"] for r in cuerpo.get("contratos_referenciados") or [] if isinstance(r, dict) and isinstance(r.get("id"), int)]
    costo = cuerpo.get("costo_usd")
    obs = Observacion(
        caso_id=caso.id,
        http_status=status,
        contratos_devueltos=ids,
        respuesta=cuerpo.get("respuesta") if isinstance(cuerpo.get("respuesta"), str) else None,
        # El proxy responde 200 también cuando el proveedor falla; ese campo lo delata.
        error=str(cuerpo["error"])[:80] if cuerpo.get("error") else None,
    )
    return obs, float(costo) if isinstance(costo, (int, float)) else 0.0, cuerpo.get("served_by")


def ejecutar(
    casos: list[Caso],
    consultar: Consultar,
    presupuesto_usd: float,
    costo_maximo_por_caso_usd: float = 0.01,
) -> tuple[list[Observacion], float, set[str]]:
    """Recorre los casos aprobados sin superar el presupuesto.

    Antes de cada consulta reserva el costo máximo esperado de un caso: si no
    cabe, se detiene. Así el tope se respeta aunque el costo real solo se conozca
    después de responder.
    """
    if presupuesto_usd <= 0:
        raise ValueError("el presupuesto debe ser positivo")
    observaciones: list[Observacion] = []
    gastado = 0.0
    modelos: set[str] = set()
    for caso in casos_evaluables(casos):
        if gastado + costo_maximo_por_caso_usd > presupuesto_usd:
            break
        obs, costo, modelo = observar(caso, consultar)
        observaciones.append(obs)
        gastado += costo
        if modelo:
            modelos.add(modelo)
    return observaciones, gastado, modelos


def correr(
    dataset: Path,
    consultar: Consultar,
    *,
    presupuesto_usd: float,
    umbral: float,
    corpus_espacio: str,
    prompt_version: str,
    config_version: int | None,
    hoy: date,
    k: int = 5,
) -> dict:
    """Corrida completa: ejecuta, puntúa y devuelve el registro comparable."""
    casos = cargar_dataset(dataset)
    aprobados = casos_evaluables(casos)
    observaciones, gastado, modelos = ejecutar(casos, consultar, presupuesto_usd)
    # Un solo modelo identifica la corrida; si hubo failover a mitad, se declara.
    modelo = next(iter(modelos)) if len(modelos) == 1 else ("mixto:" + ",".join(sorted(modelos)) if modelos else "desconocido")
    corrida = Corrida(fecha=hoy, corpus_espacio=corpus_espacio, modelo=modelo, prompt_version=prompt_version, config_version=config_version, k=k)
    registro = registro_corrida(corrida, casos, evaluar(casos, observaciones, k), umbral)
    registro["gasto_usd"] = round(gastado, 6)
    registro["presupuesto_usd"] = presupuesto_usd
    registro["casos_aprobados"] = len(aprobados)
    registro["casos_ejecutados"] = len(observaciones)
    registro["detenida_por_presupuesto"] = len(observaciones) < len(aprobados)
    return registro


def guardar(registro: dict, destino: Path) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(registro, indent=1, ensure_ascii=False), encoding="utf-8")
