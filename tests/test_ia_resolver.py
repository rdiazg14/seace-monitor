"""Resolver dinámico ia_* (IA-005): fail-soft, descifrado, TTL e invalidación.

Todo con dobles: ``get`` emula PostgREST y ``descifrar``/``master_key`` se
inyección explícita; ningún test toca red ni claves reales.
"""
from __future__ import annotations

import base64
import json

import pytest

from seace_monitor.ia.contracts import ModeloCfg
from seace_monitor.ia.crypto import cifrar_clave
from seace_monitor.ia.resolver import (
    ErrorLectura,
    ResolverCfg,
    clave_efectiva,
)

MASTER = base64.b64encode(b"K" * 32).decode()
BLOB_OK = cifrar_clave("sk-qwen-secreto", MASTER, iv=bytes(range(1, 13)))


class Resp:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload
        self.text = (
            payload if isinstance(payload, str) else json.dumps(payload)
        )

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def fila_endpoint(
    endpoint: str,
    *,
    modelo: str = "m-1",
    tipo_api: str = "openai",
    proveedor: str = "qwen",
    base_url: str = "https://p.test/v1",
    blob: str | None = BLOB_OK,
    activo_modelo: bool = True,
    activo_proveedor: bool = True,
    hereda: str | None = None,
    dimensiones: int | None = None,
    espacio: str | None = None,
    precio: dict | None = None,
    capacidades: dict | None = None,
    params: dict | None = None,
    timeout_ms: int = 45_000,
    sin_modelo: bool = False,
) -> dict:
    model_row = None if sin_modelo else {
        "modelo": modelo,
        "tipo": "generacion",
        "dimensiones": dimensiones,
        "espacio_vectorial": espacio,
        "timeout_ms": timeout_ms,
        "capacidades": capacidades or {},
        "params": params or {},
        "precio": precio or {"moneda": "USD", "in": 0.03, "out": 0.13},
        "activo": activo_modelo,
        "ia_proveedores": {
            "id": proveedor,
            "tipo_api": tipo_api,
            "base_url": base_url,
            "clave_cifrada": blob,
            "activo": activo_proveedor,
        },
    }
    return {
        "endpoint": endpoint,
        "hereda": hereda,
        "config": {},
        "activo": True,
        "ia_modelos": model_row,
    }


def make_resolver(
    filas: list[dict] | None = None,
    *,
    meta_version=7,
    meta_corpus: str | None = "gemini-emb001-1536",
    status_endpoints: int = 200,
    calls: list[str] | None = None,
    descifrar=None,
    now=lambda: 0.0,
    ttl_s: float = 60.0,
) -> ResolverCfg:
    """Resolver con ``get`` doble: endpoints/meta según la URL pedida."""
    def fake_get(url, headers=None, timeout=None):
        if calls is not None:
            calls.append(url)
        if "ia_endpoints" in url:
            if status_endpoints != 200:
                return Resp(status_endpoints, {"message": "boom"})
            return Resp(200, filas if filas is not None else [])
        if "version_config" in url:
            return Resp(200, [{"valor": meta_version}])
        if "corpus_embeddings" in url:
            if meta_corpus is None:
                return Resp(404, {"message": "no rows"})
            return Resp(200, [{"valor": meta_corpus}])
        return Resp(200, [])

    return ResolverCfg(
        url="https://db.test",
        key="service-key",
        master_key=MASTER,
        get=fake_get,
        now=now,
        descifrar=descifrar,
        ttl_s=ttl_s,
    )


def test_sin_credenciales_devuelve_none_sin_red() -> None:
    calls: list[str] = []
    resolver = ResolverCfg(url="", key="", get=lambda *a, **k: calls.append("x"))
    assert resolver.resolver("ocr") is None
    assert calls == []


def test_tabla_ausente_o_error_lectura_devuelve_none_y_cachea() -> None:
    calls: list[str] = []
    resolver = make_resolver(status_endpoints=404, calls=calls)
    assert resolver.resolver("ocr") is None
    assert resolver.resolver("ocr") is None
    # El fallo queda cacheado dentro del TTL: una llamada de red, no dos.
    assert len([c for c in calls if "ia_endpoints" in c]) == 1


def test_endpoint_activo_resuelve_modelo_proveedor_precio_y_clave() -> None:
    resolver = make_resolver([fila_endpoint(
        "ocr",
        modelo="deepseek/deepseek-ocr-2",
        proveedor="novita",
        precio={"moneda": "USD", "in": 0.03, "out": 0.03},
        capacidades={"vision": True},
        params={"foo": 1},
    )])

    cfg = resolver.resolver("ocr")

    assert cfg is not None
    assert cfg.endpoint == "ocr"
    assert cfg.proveedor == "novita"
    assert cfg.tipo_api == "openai"
    assert cfg.modelo == "deepseek/deepseek-ocr-2"
    assert cfg.base_url == "https://p.test/v1"
    assert cfg.api_key == "sk-qwen-secreto"  # descifrada solo en memoria
    assert cfg.timeout_ms == 45_000
    assert cfg.capacidades == {"vision": True}
    assert cfg.params == {"foo": 1}
    assert cfg.precio == {"moneda": "USD", "in": 0.03, "out": 0.03}
    assert cfg.version_config == 7


def test_modelo_o_proveedor_inactivo_no_resuelve() -> None:
    resolver = make_resolver([fila_endpoint("ocr", activo_modelo=False)])
    assert resolver.resolver("ocr") is None
    resolver = make_resolver([fila_endpoint("ocr", activo_proveedor=False)])
    assert resolver.resolver("ocr") is None
    resolver = make_resolver([fila_endpoint("ocr", sin_modelo=True)])
    assert resolver.resolver("ocr") is None


def test_blob_malo_deja_clave_none_y_avisa(capsys) -> None:
    resolver = make_resolver([fila_endpoint("ocr", blob="v1.malo.AA==")])
    cfg = resolver.resolver("ocr")
    assert cfg is not None and cfg.api_key is None
    assert "clave_descifrado_fallo" in capsys.readouterr().out


def test_descifrado_fallo_de_excepcion_no_expone_secreto(capsys) -> None:
    resolver = make_resolver(
        [fila_endpoint("clasificar")],
        descifrar=lambda blob, master: (_ for _ in ()).throw(
            ValueError("boom secreto")),
    )
    cfg = resolver.resolver("clasificar")
    assert cfg is not None and cfg.api_key is None
    out = capsys.readouterr().out
    assert "sk-qwen-secreto" not in out


def test_embeddings_fuera_del_espacio_del_corpus_se_descarta(capsys) -> None:
    resolver = make_resolver([fila_endpoint(
        "embeddings",
        modelo="text-embedding-v4",
        dimensiones=1536,
        espacio="qwen-tev4-1536",
    )])
    assert resolver.resolver("embeddings") is None
    assert "embeddings_fuera_de_espacio" in capsys.readouterr().out


def test_embeddings_en_espacio_del_corpus_resuelve() -> None:
    resolver = make_resolver([fila_endpoint(
        "embeddings",
        modelo="gemini-embedding-001",
        tipo_api="gemini",
        proveedor="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        dimensiones=1536,
        espacio="gemini-emb001-1536",
        blob=None,
    )])
    cfg = resolver.resolver("embeddings")
    assert cfg is not None
    assert cfg.dimensiones == 1536
    assert cfg.espacio_vectorial == "gemini-emb001-1536"


def test_hereda_copia_config_del_endpoint_origen() -> None:
    resolver = make_resolver([
        fila_endpoint("chat", modelo="qwen3.7-flash"),
        fila_endpoint("query_rewrite", hereda="chat", sin_modelo=True),
    ])
    cfg = resolver.resolver("query_rewrite")
    assert cfg is not None
    assert cfg.endpoint == "query_rewrite"
    assert cfg.modelo == "qwen3.7-flash"
    assert cfg.api_key == "sk-qwen-secreto"


def test_ttl_cachea_y_invalidar_fuerza_recarga() -> None:
    calls: list[str] = []
    reloj = {"t": 1000.0}
    resolver = make_resolver(
        [fila_endpoint("ocr")], calls=calls, now=lambda: reloj["t"])

    resolver.resolver("ocr")
    assert len([c for c in calls if "ia_endpoints" in c]) == 1

    reloj["t"] += 30  # dentro del TTL
    resolver.resolver("clasificar")
    assert len([c for c in calls if "ia_endpoints" in c]) == 1
    assert resolver.version_vista() == 7

    resolver.invalidar()
    resolver.resolver("ocr")
    assert len([c for c in calls if "ia_endpoints" in c]) == 2

    reloj["t"] += 61  # TTL expirado → recarga aunque no se invalide
    resolver.resolver("ocr")
    assert len([c for c in calls if "ia_endpoints" in c]) == 3


def test_clave_efectiva_env_fallback_solo_para_gemini(monkeypatch) -> None:
    cfg_gemini = ModeloCfg(
        endpoint="ocr", proveedor="gemini", tipo_api="gemini",
        modelo="gemini-3.7-flash", base_url="https://g.test",
        api_key=None,
    )
    cfg_otro = ModeloCfg(
        endpoint="ocr", proveedor="novita", tipo_api="openai",
        modelo="m", base_url="https://n.test", api_key=None,
    )
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    assert clave_efectiva(cfg_gemini) == "env-key"
    assert clave_efectiva(cfg_otro) is None
    cfg_gemini.api_key = "db-key"
    assert clave_efectiva(cfg_gemini) == "db-key"  # la fila gana sobre env
