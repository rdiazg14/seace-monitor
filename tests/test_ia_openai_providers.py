"""Transportes OpenAI de IA-005: clasificación, OCR y embeddings.

Dobles HTTP solamente; se verifican payload, normalización de uso, taxonomía
de errores, reintentos acotados, Retry-After y los canales de trazabilidad
compartidos con el camino Gemini.
"""
from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest

from seace_monitor.classification.openai_provider import clasificar_lote_openai
from seace_monitor.embeddings import openai_provider as emb
from seace_monitor.embeddings.gemini_provider import QuotaExceeded
from seace_monitor.embeddings.preparation import EMBED_STATS
from seace_monitor.ia.errores import ErrorProveedor
from seace_monitor.ia.openai import schema_openai, url_openai
from seace_monitor.ocr import gemini_provider as ocr_gemini
from seace_monitor.ocr.gemini_provider import CupoFlash
from seace_monitor.ocr.openai_provider import solicitar_ocr_openai


class FakeClient:
    def __init__(self, responses):
        self.responses: Iterator = iter(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs) -> httpx.Response:
        self.calls.append((url, kwargs))
        item = next(self.responses)
        if isinstance(item, Exception):
            raise item
        return item


def response(status: int, payload: dict | None = None, headers=None) -> httpx.Response:
    request = httpx.Request("POST", "https://provider.test/x")
    return httpx.Response(status, json=payload, request=request, headers=headers)


# ── clasificar ────────────────────────────────────────────────────────────────

def test_clasificar_openai_payload_uso_y_parse() -> None:
    uso: list[dict] = []
    client = FakeClient([response(200, {
        "choices": [{"message": {"content": '[{"id": 7, "categoria": "ti"}]'}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                  "total_tokens": 15},
    })])

    out = clasificar_lote_openai(
        client,
        [{"id": 7}],
        system_prompt="sys",
        schema={"type": "array", "items": {"type": "object"}},
        armar_prompt=lambda lote: "lote:" + str(len(lote)),
        api_key="sk-test",
        url="https://qwen.test/v1/chat/completions",
        parse_response=lambda text: json.loads(text),
        modelo="qwen3.7-flash",
        params={"custom": "x"},
        proveedor="qwen",
        on_success=uso.append,
    )

    assert out == [{"id": 7, "categoria": "ti"}]
    url, call = client.calls[0]
    assert url == "https://qwen.test/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    payload = call["json"]
    assert payload["model"] == "qwen3.7-flash"
    assert payload["enable_thinking"] is False
    assert payload["custom"] == "x"
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["messages"][0] == {"role": "system", "content": "sys"}
    # La cuota C4 recibe el body normalizado a usageMetadata.
    assert uso == [{"usageMetadata": {
        "promptTokenCount": 10,
        "candidatesTokenCount": 5,
        "totalTokenCount": 15,
    }}]


def test_clasificar_openai_429_retry_after_y_reintento() -> None:
    sleeps: list[float] = []
    client = FakeClient([
        response(429, {"error": {"type": "rate_limit"}},
                 headers={"Retry-After": "5"}),
        response(200, {
            "choices": [{"message": {"content": "[]"}}],
            "usage": {},
        }),
    ])

    assert clasificar_lote_openai(
        client, [{"id": 1}],
        system_prompt="s", schema={}, armar_prompt=lambda l: "p",
        api_key="k", url="u", parse_response=json.loads,
        backoff=(0.0,), sleep=sleeps.append,
    ) == []
    assert sleeps == [5.0]


def test_clasificar_openai_401_no_reintenta() -> None:
    client = FakeClient([response(401, {"error": {"type": "auth"}})])
    with pytest.raises(ErrorProveedor) as caught:
        clasificar_lote_openai(
            client, [{"id": 1}],
            system_prompt="s", schema={}, armar_prompt=lambda l: "p",
            api_key="k", url="u", parse_response=json.loads,
            sleep=lambda s: None,
        )
    assert caught.value.kind == "credencial"
    assert len(client.calls) == 1


def test_clasificar_openai_cuota_no_reintenta() -> None:
    client = FakeClient([response(402, {
        "error": {"type": "insufficient_quota", "message": "sin saldo"},
    })])
    with pytest.raises(ErrorProveedor) as caught:
        clasificar_lote_openai(
            client, [{"id": 1}],
            system_prompt="s", schema={}, armar_prompt=lambda l: "p",
            api_key="k", url="u", parse_response=json.loads,
            sleep=lambda s: None,
        )
    assert caught.value.kind == "cuota"
    assert len(client.calls) == 1


def test_clasificar_openai_errores_agotan_reintentos() -> None:
    sleeps: list[float] = []
    client = FakeClient([
        response(500, {}), response(500, {}), response(500, {}),
    ])
    with pytest.raises(RuntimeError, match="clasificar_lote fallo"):
        clasificar_lote_openai(
            client, [{"id": 1}],
            system_prompt="s", schema={}, armar_prompt=lambda l: "p",
            api_key="k", url="u", parse_response=json.loads,
            backoff=(0.0, 0.0), sleep=sleeps.append,
        )
    assert len(client.calls) == 3


def test_schema_openai_retira_propertyordering() -> None:
    out = schema_openai({
        "type": "object",
        "propertyOrdering": ["a"],
        "properties": {"a": {"type": "string", "propertyOrdering": []}},
    })
    assert out["type"] == "json_schema"
    schema = out["json_schema"]["schema"]
    assert "propertyOrdering" not in schema
    assert "propertyOrdering" not in schema["properties"]["a"]


def test_url_openai_sin_doble_barra() -> None:
    assert url_openai("https://x.test/v1/", "chat/completions") == (
        "https://x.test/v1/chat/completions")


# ── OCR ───────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_ocr_usage() -> Iterator[None]:
    ocr_gemini.LAST_OCR_USAGE.clear()
    ocr_gemini.OCR_USAGE_ACUM.update(
        {"prompt": 0, "candidates": 0, "total": 0, "llamadas": 0})
    yield
    ocr_gemini.LAST_OCR_USAGE.clear()
    ocr_gemini.OCR_USAGE_ACUM.update(
        {"prompt": 0, "candidates": 0, "total": 0, "llamadas": 0})


def test_ocr_openai_imagen_data_uri_y_uso_compartido() -> None:
    client = FakeClient([response(200, {
        "choices": [{"message": {"content": "texto ocr"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 40,
                  "total_tokens": 140},
    })])

    text = solicitar_ocr_openai(
        client, b"img", "image/jpeg", "sk-test",
        url="https://novita.test/openai/chat/completions",
        modelo="deepseek/deepseek-ocr-2", proveedor="novita",
    )

    assert text == "texto ocr"
    payload = client.calls[0][1]["json"]
    content = payload["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    # Mismo canal de uso que el transporte Gemini.
    assert ocr_gemini.LAST_OCR_USAGE == {
        "prompt": 100, "candidates": 40, "total": 140}
    assert ocr_gemini.OCR_USAGE_ACUM["llamadas"] == 1


def test_ocr_openai_429_traduce_a_cupoflash_reanudable() -> None:
    client = FakeClient([response(429, {"error": {"type": "rate_limit"}})])
    with pytest.raises(CupoFlash) as caught:
        solicitar_ocr_openai(
            client, b"img", "image/jpeg", "k", url="u", sleep=lambda s: None)
    assert caught.value.motivo == "429"
    assert len(client.calls) == 1


def test_ocr_openai_cuota_traduce_a_cupoflash() -> None:
    client = FakeClient([response(402, {
        "error": {"type": "insufficient_quota", "message": "saldo"},
    })])
    with pytest.raises(CupoFlash) as caught:
        solicitar_ocr_openai(
            client, b"img", "image/jpeg", "k", url="u", sleep=lambda s: None)
    assert caught.value.motivo == "cuota"


def test_ocr_openai_server_reintenta_y_luego_falla() -> None:
    client = FakeClient([
        response(500, {}), response(500, {}), response(500, {}),
        response(500, {}),
    ])
    with pytest.raises(RuntimeError, match="OCR OpenAI fallo"):
        solicitar_ocr_openai(
            client, b"img", "image/jpeg", "k", url="u", sleep=lambda s: None)
    assert len(client.calls) == 4  # GEMINI_OCR_BACKOFF: 4 intentos


# ── embeddings ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_embed_stats() -> Iterator[None]:
    EMBED_STATS.update({"requests": 0, "texts": 0, "chars": 0, "tokens_api": 0})
    yield
    EMBED_STATS.update({"requests": 0, "texts": 0, "chars": 0, "tokens_api": 0})


def _embed_body(n: int, dim: int = 4) -> dict:
    return {
        "data": [{"index": i, "embedding": [0.5] * dim} for i in range(n)],
        "usage": {"total_tokens": n * 3},
    }


def test_embeddings_openai_lotes_dimension_y_stats() -> None:
    client = FakeClient([response(200, _embed_body(2)), response(200, _embed_body(1))])

    out = emb.solicitar_embeddings_openai(
        client, ["a", "b", "c"], "sk-test",
        url="https://qwen.test/v1/embeddings",
        modelo="text-embedding-v4", dimensiones=4, batch_max=2,
        proveedor="qwen",
    )

    assert len(out) == 3
    # L2-normalizado: norma ~1 (0.5*4 → vector unitario de 0.5s tras norma).
    assert all(abs(sum(v * v for v in vec) ** 0.5 - 1.0) < 1e-6 for vec in out)
    assert len(client.calls) == 2
    payload = client.calls[0][1]["json"]
    assert payload["model"] == "text-embedding-v4"
    assert payload["dimensions"] == 4
    assert payload["input"] == ["a", "b"]
    assert EMBED_STATS["requests"] == 2
    assert EMBED_STATS["tokens_api"] == 9


def test_embeddings_openai_dim_incorrecta_no_reintenta() -> None:
    client = FakeClient([response(200, {
        "data": [{"index": 0, "embedding": [1.0, 2.0, 3.0]}],
        "usage": {},
    })])
    with pytest.raises(ErrorProveedor) as caught:
        emb.solicitar_embeddings_openai(
            client, ["a"], "k", url="u", modelo="m", dimensiones=4,
            sleep=lambda s: None,
        )
    assert caught.value.kind == "invalid_json"
    assert len(client.calls) == 1


def test_embeddings_openai_cuota_aborta_siempre() -> None:
    client = FakeClient([response(402, {
        "error": {"type": "insufficient_quota", "message": "saldo"},
    })])
    with pytest.raises(QuotaExceeded):
        emb.solicitar_embeddings_openai(
            client, ["a"], "k", url="u", modelo="m", dimensiones=4,
            sleep=lambda s: None,
        )
    assert len(client.calls) == 1


def test_embeddings_openai_429_fail_fast_quota_y_retry_after() -> None:
    sleeps: list[float] = []
    client = FakeClient([response(
        429, {"error": {"type": "rate_limit"}}, headers={"Retry-After": "3"})])
    with pytest.raises(QuotaExceeded):
        emb.solicitar_embeddings_openai(
            client, ["a"], "k", url="u", modelo="m", dimensiones=4,
            fail_fast=True, sleep=sleeps.append,
        )
    assert sleeps == []  # fail_fast: sin espera

    client = FakeClient([
        response(429, {}, headers={"Retry-After": "3"}),
        response(200, _embed_body(1)),
    ])
    out = emb.solicitar_embeddings_openai(
        client, ["a"], "k", url="u", modelo="m", dimensiones=4,
        sleep=sleeps.append,
    )
    assert len(out) == 1
    assert 3.0 in sleeps
