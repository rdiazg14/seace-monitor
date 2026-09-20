"""Cliente Supabase centralizado.

Reemplaza el patrón repetido en cada script de:

    from supabase import create_client
    supa = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

por un único punto. La semántica de error se conserva: si faltan credenciales,
se lanza ``SystemExit`` con el mismo mensaje que usaban los scripts, para no
cambiar el comportamiento ante entornos sin secretos.
"""
from __future__ import annotations

from supabase import create_client

from .config import obtener_strip


def crear_cliente(url: str | None = None, key: str | None = None):
    """Crea el cliente supabase-py.

    Sin argumentos usa ``SUPABASE_URL`` + ``SUPABASE_SERVICE_KEY`` del entorno.
    Lanza ``SystemExit`` (mismo mensaje histórico) si faltan credenciales.
    """
    url = (url or obtener_strip("SUPABASE_URL")).strip()
    key = (key or obtener_strip("SUPABASE_SERVICE_KEY")).strip()
    if not url or not key:
        raise SystemExit("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY no encontrados")
    return create_client(url, key)
