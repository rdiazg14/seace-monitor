"""Composición IA-005: entrypoints ante config dinámica vs camino por env.

Dobles únicamente; ``resolver_cfg``/``cfg_resuelta`` se inyectan como costura.
Verifica preservación de comportamiento (env), selección de proveedor,
guardrail de corpus y propagación de modelo/precio a cuota y trazabilidad.
"""
from __future__ import annotations

import pytest

import clasificar_gemini as cl
import descargar_requerimiento as dr
import generar_embeddings as ge
import extraer_contenedores as ec
from seace_monitor.ia.contracts import ModeloCfg
from seace_monitor.ia import pipeline
from seace_monitor.ocr import gemini_provider as ocr_provider
from seace_monitor.ocr.gemini_provider import OCR_ACTIVO


def cfg_openai(**kw) -> ModeloCfg:
    base = dict(
        endpoint="ocr",
        proveedor="novita",
        tipo_api="openai",
        modelo="deepseek/deepseek-ocr-2",
        base_url="https://novita.test/openai",
        api_key="db-key",
        timeout_ms=45_000,
        precio={"moneda": "USD", "in": 0.03, "out": 0.03},
        version_config=9,
    )
    base.update(kw)
    return ModeloCfg(**base)


@pytest.fixture(autouse=True)
def reset_ocr_activo():
    yield
    OCR_ACTIVO.update({
        "modelo": ocr_provider.GEMINI_FLASH,
        "usd_in": 0.75,
        "usd_out": 3.75,
        "version_config": 0,
        "precio": {"in": 0.75, "out": 3.75, "tramos": []},
        "precio_fuente": "tabla",
    })


# ── helpers de pipeline ───────────────────────────────────────────────────────

def test_cfg_con_credencial_none_sin_config(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "resolver_compartido",
                        lambda: type("R", (), {"resolver": lambda s, e: None})())
    assert pipeline.cfg_con_credencial("ocr") is None


def test_cfg_con_credencial_sin_clave_devuelve_none(monkeypatch, capsys) -> None:
    cfg = cfg_openai(api_key=None, proveedor="novita")
    monkeypatch.setattr(
        pipeline, "resolver_compartido",
        lambda: type("R", (), {"resolver": lambda s, e: cfg})(),
    )
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert pipeline.cfg_con_credencial("ocr") is None
    assert "sin clave utilizable" in capsys.readouterr().out


def test_fijar_ocr_activo_propaga_modelo_y_precio() -> None:
    pipeline.fijar_ocr_activo(cfg_openai())
    assert OCR_ACTIVO["modelo"] == "deepseek/deepseek-ocr-2"
    assert OCR_ACTIVO["usd_in"] == 0.03
    assert OCR_ACTIVO["usd_out"] == 0.03


def test_extras_embeddings_dim_distinta_aborta() -> None:
    cfg = cfg_openai(
        endpoint="embeddings", tipo_api="openai", modelo="text-embedding-v4",
        dimensiones=3072, espacio_vectorial="qwen-tev4-3072",
    )
    with pytest.raises(SystemExit, match="dimensiones"):
        pipeline.extras_embeddings(cfg)


def test_extras_embeddings_openai_arma_transporte() -> None:
    cfg = cfg_openai(
        endpoint="embeddings", proveedor="qwen", tipo_api="openai",
        modelo="text-embedding-v4", dimensiones=1536,
        espacio_vectorial="qwen-tev4-1536",
        capacidades={"batch_max": 10}, precio={"in": 0.07},
    )
    extras = pipeline.extras_embeddings(cfg)
    assert extras["api_key"] == "db-key"
    assert extras["modelo"] == "text-embedding-v4"
    assert extras["precio_in"] == 0.07
    assert extras["version_config"] == 9
    assert "solicitar" in extras


def test_extras_embeddings_sin_cfg_vacio() -> None:
    assert pipeline.extras_embeddings(None) == {}


# ── descargar_requerimiento ──────────────────────────────────────────────────

def test_dr_ocr_env_path_sin_cfg(monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(dr, "GEMINI_API_KEY", "runtime-key")
    monkeypatch.setattr(dr, "resolver_cfg", lambda e: None)
    monkeypatch.setattr(dr, "respetar_rpm", lambda: calls.append(("rpm",)))
    monkeypatch.setattr(
        dr, "solicitar_ocr_gemini",
        lambda client, b, m, k: calls.append(("gemini", k)) or " texto  ",
    )
    assert dr.ocr_pagina_gemini(b"x") == "texto"
    assert calls == [("rpm",), ("gemini", "runtime-key")]


def test_dr_ocr_cfg_openai_desplaza_env(monkeypatch) -> None:
    calls: list[tuple] = []
    cfg = cfg_openai()
    monkeypatch.setattr(dr, "GEMINI_API_KEY", "")
    monkeypatch.setattr(dr, "resolver_cfg", lambda e: cfg)
    monkeypatch.setattr(dr, "respetar_rpm", lambda: calls.append(("rpm",)))

    def fake_openai(client, img, mime, key, **kw):
        calls.append(("openai", key, kw["modelo"], kw["url"]))
        return " ocr cfg "

    monkeypatch.setattr(
        "seace_monitor.ocr.openai_provider.solicitar_ocr_openai", fake_openai)
    # solicitar_ocr_cfg importa el símbolo dentro de la función; el parcheo
    # del atributo del módulo basta para interceptar el transporte.
    assert dr.ocr_pagina_gemini(b"x") == "ocr cfg"
    assert calls == [
        ("rpm",),
        ("openai", "db-key", "deepseek/deepseek-ocr-2",
         "https://novita.test/openai/chat/completions"),
    ]
    assert OCR_ACTIVO["modelo"] == "deepseek/deepseek-ocr-2"


def test_dr_rechunk_usa_cfg_embeddings(monkeypatch) -> None:
    calls: list[dict] = []
    cfg = cfg_openai(
        endpoint="embeddings", proveedor="gemini", tipo_api="gemini",
        modelo="gemini-embedding-001",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="gkey-db", dimensiones=1536,
        espacio_vectorial="gemini-emb001-1536",
    )
    monkeypatch.setattr(dr, "resolver_cfg", lambda e: cfg)
    monkeypatch.setattr(dr, "cfg_resuelta", lambda e: cfg)

    def fake_postprocess(supa, cid, **kw):
        calls.append(kw)

    monkeypatch.setattr(dr, "_rechunk_embed_pdf", fake_postprocess)
    dr.rechunk_embed_pdf(object(), 5)
    assert calls[0]["api_key"] == "gkey-db"
    assert calls[0]["modelo"] == "gemini-embedding-001"
    assert calls[0]["version_config"] == 9


def test_dr_rechunk_sin_cfg_usa_env(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(dr, "GEMINI_API_KEY", "env-key")
    monkeypatch.setattr(dr, "cfg_resuelta", lambda e: None)
    monkeypatch.setattr(
        dr, "_rechunk_embed_pdf", lambda supa, cid, **kw: calls.append(kw))
    dr.rechunk_embed_pdf(object(), 5)
    assert calls == [{"api_key": "env-key"}]


# ── clasificar_gemini ─────────────────────────────────────────────────────────

class Args:
    max_llamadas_dia = 120


def test_clasificar_construir_config_env_sin_cfg(monkeypatch) -> None:
    monkeypatch.setattr(cl, "resolver_cfg", lambda e: None)
    cfg = cl.construir_config(Args())
    assert cfg.modelo == cl.GEMINI_FLASH
    assert cfg.transporte is None


def test_clasificar_construir_config_openai(monkeypatch) -> None:
    cfg_ia = cfg_openai(
        endpoint="clasificar", proveedor="qwen", modelo="qwen3.7-flash",
        base_url="https://qwen.test/v1", params={"enable_thinking": False},
        tipo_api="openai",
    )
    monkeypatch.setattr(cl, "resolver_cfg", lambda e: cfg_ia)
    cfg = cl.construir_config(Args())
    assert cfg.modelo == "qwen3.7-flash"
    assert cfg.api_key == "db-key"
    assert cfg.url == "https://qwen.test/v1/chat/completions"
    assert cfg.transporte is not None
    assert cfg.timeout == 45.0


def test_clasificar_construir_config_gemini_dinamico(monkeypatch) -> None:
    cfg_ia = cfg_openai(
        endpoint="clasificar", proveedor="gemini", tipo_api="gemini",
        modelo="gemini-3.1-flash-lite",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="gkey",
    )
    monkeypatch.setattr(cl, "resolver_cfg", lambda e: cfg_ia)
    cfg = cl.construir_config(Args())
    assert cfg.modelo == "gemini-3.1-flash-lite"
    assert cfg.url.endswith("/models/gemini-3.1-flash-lite:generateContent")
    assert cfg.transporte is None


def test_clasificar_cfg_sin_clave_cae_a_env(monkeypatch, capsys) -> None:
    cfg_ia = cfg_openai(endpoint="clasificar", api_key=None)
    monkeypatch.setattr(cl, "resolver_cfg", lambda e: cfg_ia)
    cfg = cl.construir_config(Args())
    assert cfg.modelo == cl.GEMINI_FLASH  # env fallback
    assert cfg.transporte is None


# ── generar_embeddings ────────────────────────────────────────────────────────

def test_ge_run_gemini_env_sin_cfg(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(ge, "GEMINI_API_KEY", "env-key")
    monkeypatch.setattr(ge, "cfg_resuelta", lambda e: None)
    monkeypatch.setattr(
        ge, "_run_gemini", lambda supa, limit, **kw: calls.append(kw) or {})
    ge.run_gemini(object(), 0)
    assert calls[0]["api_key"] == "env-key"
    assert "solicitar" not in calls[0]


def test_ge_run_gemini_con_cfg(monkeypatch) -> None:
    calls: list[dict] = []
    cfg = cfg_openai(
        endpoint="embeddings", proveedor="qwen", tipo_api="openai",
        modelo="text-embedding-v4", dimensiones=1536,
        espacio_vectorial="qwen-tev4-1536",
    )
    monkeypatch.setattr(ge, "cfg_resuelta", lambda e: cfg)
    monkeypatch.setattr(
        ge, "_run_gemini", lambda supa, limit, **kw: calls.append(kw) or {})
    ge.run_gemini(object(), 0)
    assert calls[0]["api_key"] == "db-key"
    assert calls[0]["modelo"] == "text-embedding-v4"
    assert "solicitar" in calls[0]


# ── extraer_contenedores ─────────────────────────────────────────────────────

def test_ec_ocr_page_none_sin_credenciales(monkeypatch) -> None:
    monkeypatch.setattr(ec, "GEMINI_API_KEY", "")
    monkeypatch.setattr(ec, "cfg_con_credencial", lambda e, **kw: None)
    monkeypatch.setattr(ec, "cfg_resuelta", lambda e: None)
    assert ec.ia_habilitada() is False


def test_ec_ocr_page_habilitada_con_cfg(monkeypatch) -> None:
    monkeypatch.setattr(ec, "GEMINI_API_KEY", "")
    monkeypatch.setattr(ec, "cfg_con_credencial", lambda e, **kw: cfg_openai())
    assert ec.ia_habilitada() is True


def test_ec_ocr_pagina_despacha_cfg_openai(monkeypatch) -> None:
    calls: list[tuple] = []
    cfg = cfg_openai()
    monkeypatch.setattr(ec, "GEMINI_API_KEY", "")
    monkeypatch.setattr(ec, "cfg_resuelta", lambda e: cfg)

    def fake_openai(client, img, mime, key, **kw):
        calls.append((key, kw.get("modelo")))
        return "texto"

    monkeypatch.setattr(
        "seace_monitor.ocr.openai_provider.solicitar_ocr_openai", fake_openai)
    assert ec.ocr_pagina_gemini(b"img", "image/jpeg") == "texto"
    assert calls == [("db-key", "deepseek/deepseek-ocr-2")]


def test_ec_ocr_pagina_env_sin_cfg(monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(ec, "GEMINI_API_KEY", "env-key")
    monkeypatch.setattr(ec, "cfg_resuelta", lambda e: None)
    monkeypatch.setattr(
        ec, "solicitar_ocr_gemini",
        lambda client, b, m, k: calls.append(k) or "nativo",
    )
    assert ec.ocr_pagina_gemini(b"img", "image/png") == "nativo"
    assert calls == ["env-key"]
