"""Carga de configuración y secretos desde el entorno + `.env` local.

Consolida el bloque que estaba copiado en ~59 scripts:

    _env = Path(__file__).parent / ".env"
    if _env.exists():
        for line in _env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

La semántica se preserva exactamente: `.env` **solo** rellena variables que NO
están ya en el entorno (`setdefault`), para que GitHub Secrets / entorno tengan
prioridad sobre el `.env` local. El `.env` nunca se commitea.
"""
from __future__ import annotations

import os
from pathlib import Path

# El `.env` vive SIEMPRE en la raíz del repo (no al lado de cada script).
# config.py está en seace_monitor/, así que sube dos niveles para llegar a la raíz.
RAIZ_REPO = Path(__file__).resolve().parent.parent
ENV_FILENAME = ".env"


def cargar_env(root: Path | None = None, nombre: str = ENV_FILENAME) -> None:
    """Lee ``root/<nombre>`` y hace ``setdefault`` de cada ``clave=valor``.

    No pisa variables ya definidas en el entorno. Ignora comentarios y líneas
    sin ``=``. Si el archivo no existe, no hace nada (fail-soft).
    """
    base = root or RAIZ_REPO
    env_file = base / nombre
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def obtener(nombre: str, default: str = "") -> str:
    """getenv a ``str`` con default vacío."""
    return os.environ.get(nombre, default)


def obtener_strip(nombre: str, default: str = "") -> str:
    """getenv + strip, con default vacío."""
    return (os.environ.get(nombre) or default).strip()
