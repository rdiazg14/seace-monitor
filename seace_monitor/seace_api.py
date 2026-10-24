"""Constantes y helpers compartidos para hablar con la API pública de SEACE.

Centraliza los endpoints de prod6.seace.gob.pe y el parseo de fechas que se
repiten en los scripts de ingesta/enriquecimiento/refresh. Son constantes y
funciones puras (sin Playwright ni I/O), por lo que no rompen el flujo de los
scripts que lanzan un navegador por separado.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import datetime

# Página pública del buscador y base de la API JSON.
SPA_BASE = "https://prod6.seace.gob.pe"
SPA_URL = SPA_BASE + "/buscador-publico/contrataciones"

# Listado/búsqueda paginada (buscador).
API_BUSCADOR = SPA_BASE + "/v1/s8uit-services/buscadorpublico/contrataciones/buscador"
# Detalle completo de un contrato (listar-completo?id_contrato=N).
API_DETALLE = SPA_BASE + "/v1/s8uit-services/buscadorpublico/contrataciones/listar-completo"

# Nombre con el que este proyecto se identifica ante el portal (cabecera
# User-Agent) en todas sus peticiones: dice quién consulta y que es una
# consulta automatizada. Desde el 18/10/2026 el portal rechaza con 403 los
# nombres por defecto de las herramientas (python-httpx, HeadlessChrome).
IDENTIFICACION = "Enertronic/1.0 (consulta automatizada del buscador publico)"

# Pausa mínima entre peticiones de detalle y de archivos. El portal rechaza
# por un tiempo la dirección que pide en ráfaga (23/10/2026: 403 tras unas
# 13 descargas seguidas sin pausa). Ajustable con SEACE_PAUSA_SEGUNDOS.
PAUSA_SEGUNDOS_DEFECTO = 10.0


def pausa_configurada() -> float:
    valor = (os.getenv("SEACE_PAUSA_SEGUNDOS") or "").strip()
    try:
        return max(0.0, float(valor)) if valor else PAUSA_SEGUNDOS_DEFECTO
    except ValueError:
        return PAUSA_SEGUNDOS_DEFECTO


class Ritmo:
    """Garantiza un intervalo mínimo entre peticiones consecutivas.

    La primera petición no espera; las siguientes esperan solo lo que falte
    para completar el intervalo desde la anterior.
    """

    def __init__(
        self,
        intervalo: float | None = None,
        *,
        reloj: Callable[[], float] = time.monotonic,
        dormir: Callable[[float], None] = time.sleep,
    ):
        self.intervalo = pausa_configurada() if intervalo is None else intervalo
        self._reloj = reloj
        self._dormir = dormir
        self._ultima: float | None = None

    def esperar(self) -> None:
        if self._ultima is not None:
            falta = self.intervalo - (self._reloj() - self._ultima)
            if falta > 0:
                self._dormir(falta)
        self._ultima = self._reloj()


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
