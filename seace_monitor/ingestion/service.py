"""Transformaciones puras de registros SEACE a contratos persistibles."""

from __future__ import annotations

from collections.abc import Iterable

from seace_monitor.seace_api import parsear_fecha


def preparar_fila_db(registro: dict) -> dict:
    """Convierte hechos de la API sin incorporar clasificación inferida."""
    return {
        "id": registro["idContrato"],
        "nro_contratacion": str(registro.get("nroContratacion", "")),
        "descripcion_contrato": registro.get("desContratacion"),
        "objeto": registro.get("nomObjetoContrato"),
        "descripcion": registro.get("desObjetoContrato"),
        "entidad": registro.get("nomEntidad"),
        "estado": registro.get("nomEstadoContrato"),
        "fecha_publica": parsear_fecha(registro.get("fecPublica")),
        "fecha_ini_cotizacion": parsear_fecha(registro.get("fecIniCotizacion")),
        "fecha_fin_cotizacion": parsear_fecha(registro.get("fecFinCotizacion")),
        "tipo_cotizacion": str(registro.get("idTipoCotizacion", "")),
        "cotizar": bool(registro.get("cotizar", False)),
    }


def _id_valido(registro) -> int | None:
    try:
        value = int(registro.get("idContrato"))
    except (AttributeError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def ids_de_lote(lote: Iterable[dict]) -> list[int]:
    """Ids válidos de una página del listado, sin repetir, en su orden."""
    salida: list[int] = []
    vistos: set[int] = set()
    for registro in lote:
        cid = _id_valido(registro)
        if cid is not None and cid not in vistos:
            vistos.add(cid)
            salida.append(cid)
    return salida


def seleccionar_altas(
    lote: Iterable[dict],
    existentes: set[int],
    tomados: set[int],
) -> list[dict]:
    """Registros de la página que no están en la base ni se tomaron antes.

    El listado (orden=2) viene por fecha de publicación descendente, no por
    idContrato: un proceso publicado hoy puede tener un id menor que el
    máximo guardado. Por eso el alta se decide por presencia. ``tomados``
    acumula los ids de la corrida (el listado repite ids entre páginas) y se
    actualiza aquí. Los registros sin id válido se descartan, como antes.
    """
    altas: list[dict] = []
    for registro in lote:
        cid = _id_valido(registro)
        if cid is None or cid in existentes or cid in tomados:
            continue
        tomados.add(cid)
        altas.append(registro)
    return altas


def motivo_parada(
    *,
    pagina: int,
    total_paginas: int,
    lote_vacio: bool,
    paginas_sin_altas: int,
    paginas_conocidas: int,
    max_paginas: int,
) -> str | None:
    """Por qué se deja de paginar tras leer ``pagina``; None si se sigue.

    ``paginas_conocidas`` y ``max_paginas`` en 0 desactivan su criterio
    (modo completo: solo corta el fin del listado, como antes).
    """
    if lote_vacio:
        return "pagina_vacia"
    if pagina >= total_paginas:
        return "ultima_pagina"
    if paginas_conocidas and paginas_sin_altas >= paginas_conocidas:
        return "paginas_conocidas"
    if max_paginas and pagina >= max_paginas:
        return "tope_paginas"
    return None
