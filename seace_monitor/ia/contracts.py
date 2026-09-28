"""Contratos neutros de configuración IA — espejo de IA-003 (``src/ia/contracts.ts``).

``ModeloCfg`` es la configuración resuelta de un endpoint ``ia_endpoints``
(× ``ia_modelos`` × ``ia_proveedores``). ``api_key`` vive solo en memoria:
nunca a logs, artefactos ni caché compartida.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ModeloCfg:
    """Configuración efectiva de un endpoint IA resuelta desde ``ia_*``."""

    endpoint: str          # 'clasificar' | 'ocr' | 'embeddings' | ...
    proveedor: str         # 'qwen' | 'novita' | 'gemini' | 'cloudflare'
    tipo_api: str          # 'openai' | 'gemini' | 'nativo'
    modelo: str            # id tal como lo exige la API
    base_url: str | None   # None para 'nativo'
    # repr=False: la clave descifrada nunca aparece en prints/logs/str(cfg).
    api_key: str | None = field(default=None, repr=False)
    timeout_ms: int = 60_000
    capacidades: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    dimensiones: int | None = None        # embeddings: dimensión efectiva
    espacio_vectorial: str | None = None  # embeddings: identidad del espacio
    precio: dict = field(default_factory=dict)  # {"moneda","in","out",...}
    version_config: int = 0
