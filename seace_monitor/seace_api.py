"""Constantes y helpers compartidos para hablar con la API pública de SEACE.

Centraliza los endpoints de prod6.seace.gob.pe y el parseo de fechas que se
repiten en los scripts de ingesta/enriquecimiento/refresh. Son constantes y
funciones puras (sin Playwright ni I/O), por lo que no rompen el flujo de los
scripts que lanzan un navegador por separado.
"""
from __future__ import annotations

from datetime import datetime

# Página pública del buscador y base de la API JSON.
SPA_BASE = "https://prod6.seace.gob.pe"
SPA_URL = SPA_BASE + "/buscador-publico/contrataciones"

# Listado/búsqueda paginada (buscador).
API_BUSCADOR = SPA_BASE + "/v1/s8uit-services/buscadorpublico/contrataciones/buscador"
# Detalle completo de un contrato (listar-completo?id_contrato=N).
API_DETALLE = SPA_BASE + "/v1/s8uit-services/buscadorpublico/contrataciones/listar-completo"

_FMT_SEACE = "%d/%m/%Y %H:%M:%S"


def parsear_fecha(s: str | None) -> str | None:
    """'dd/mm/yyyy HH:MM:SS' (pared Lima) → ISO 8601 con offset -05:00.

    Perú no tiene DST, por eso el offset es constante. Se descartan fechas
    corruptas que el SEACE entrega en contratos legacy (p. ej.
    fecFinCotizacion con año 2052/2206/4202): año fuera de [2000, año+2].
    """
    if not s:
        return None
    try:
        dt = datetime.strptime(s.strip(), _FMT_SEACE)
    except Exception:
        return None
    if dt.year < 2000 or dt.year > datetime.now().year + 2:
        return None
    return dt.isoformat() + "-05:00"
