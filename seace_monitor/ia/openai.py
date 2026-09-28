"""Transporte OpenAI-compatible compartido — espejo de IA-003 (``providers/openai``).

Sirve a Qwen (Model Studio, modo compatible) y Novita. Contiene solo el
plumbing HTTP, la normalización de ``usage`` a la forma ``usageMetadata`` que
ya consumen cuotas y estadísticas, y la traducción de response_schema de
Gemini a ``json_schema`` de OpenAI. Las políticas de reintento y cuota quedan
en cada dominio.
"""
from __future__ import annotations

import httpx

from seace_monitor.ia.errores import (
    ErrorProveedor,
    clasificar_http,
    error_red,
    error_timeout,
)


def post_openai(
    client: httpx.Client,
    url: str,
    api_key: str,
    body: dict,
    *,
    timeout: float,
    proveedor: str,
    modelo: str | None = None,
) -> dict:
    """POST OpenAI-compatible; traduce HTTP/red/timeout a ``ErrorProveedor``."""
    try:
        response = client.post(
            url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json=body,
            timeout=timeout,
        )
    except httpx.TimeoutException as error:
        raise error_timeout(proveedor, modelo) from error
    except httpx.HTTPError as error:
        raise error_red(error, proveedor, modelo) from error
    if response.status_code < 200 or response.status_code >= 300:
        retry_after: float | None = None
        raw_ra = response.headers.get("Retry-After")
        if raw_ra:
            try:
                retry_after = float(raw_ra)
            except ValueError:
                retry_after = None
        raise clasificar_http(
            response.status_code,
            response.text[:400] if response.text else "",
            proveedor,
            modelo,
            retry_after=retry_after,
        )
    try:
        data = response.json()
    except ValueError:
        raise ErrorProveedor(
            "invalid_json",
            f"{proveedor} cuerpo no-JSON{f' [{modelo}]' if modelo else ''}",
            proveedor=proveedor, modelo=modelo, retriable=False,
        ) from None
    if not isinstance(data, dict):
        raise ErrorProveedor(
            "invalid_json",
            f"{proveedor} JSON no-objeto{f' [{modelo}]' if modelo else ''}",
            proveedor=proveedor, modelo=modelo, retriable=False,
        )
    return data


def usage_metadata(body: dict) -> dict:
    """Normaliza ``usage`` OpenAI a la forma ``usageMetadata`` de Gemini.

    ``acumular_tokens`` y ``registrar_llamada_c4`` leen ``promptTokenCount`` /
    ``candidatesTokenCount`` / ``totalTokenCount``; la misma trazabilidad queda
    intacta sin duplicar contadores por proveedor.
    """
    usage = body.get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or prompt + completion)
    return {
        "usageMetadata": {
            "promptTokenCount": prompt,
            "candidatesTokenCount": completion,
            "totalTokenCount": total,
        }
    }


def mensaje_texto(data: dict, proveedor: str, modelo: str | None = None) -> str:
    """Extrae ``choices[0].message.content``; vacío → error reintentable."""
    choices = data.get("choices") or []
    content = ""
    if choices:
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "") for part in content if isinstance(part, dict)
            )
    if not content:
        raise ErrorProveedor(
            "vacio",
            f"{proveedor} respuesta vacía{f' [{modelo}]' if modelo else ''}",
            proveedor=proveedor, modelo=modelo, retriable=True,
        )
    return content


def schema_openai(schema: dict) -> dict:
    """Traduce un ``responseSchema`` Gemini a ``json_schema`` OpenAI.

    Los contratos de clasificación ya usan tipos JSON Schema en minúsculas;
    se retira ``propertyOrdering`` (extensión Gemini) y se envuelve en la
    forma ``{"type": "json_schema", "json_schema": {...}}``.
    """
    def limpiar(nodo):
        if isinstance(nodo, dict):
            return {
                clave: limpiar(valor)
                for clave, valor in nodo.items()
                if clave != "propertyOrdering"
            }
        if isinstance(nodo, list):
            return [limpiar(item) for item in nodo]
        return nodo

    return {
        "type": "json_schema",
        "json_schema": {
            "name": "respuesta",
            "schema": limpiar(schema),
            "strict": False,
        },
    }


def url_openai(base_url: str, path: str) -> str:
    """``{base_url}/chat/completions`` — sin doble barra."""
    return f"{base_url.rstrip('/')}/{path}"
