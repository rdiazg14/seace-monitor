"""Transformaciones puras de registros SEACE a contratos persistibles."""

from __future__ import annotations

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
