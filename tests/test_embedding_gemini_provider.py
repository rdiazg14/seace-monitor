"""Contrato del transporte Gemini de embeddings sin consumir la API real."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

import generar_embeddings as entrypoint
from seace_monitor.embeddings import gemini_provider as provider
from seace_monitor.embeddings.preparation import EMBED_STATS, reset_embed_stats


class FakeClient:
    def __init__(self, responses: list[httpx.Response]):
        self.responses: Iterator[httpx.Response] = iter(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs) -> httpx.Response:
        self.calls.append((url, kwargs))
        return next(self.responses)


def response(status: int, *, json: dict | None = None, headers: dict | None = None):
    request = httpx.Request("POST", "https://gemini.test/embedding")
    return httpx.Response(status, json=json, headers=headers, request=request)


@pytest.fixture(autouse=True)
def clean_stats() -> Iterator[None]:
    reset_embed_stats()
    yield
    reset_embed_stats()


def test_lote_envia_contrato_y_normaliza_respuesta(monkeypatch) -> None:
    monkeypatch.setattr(provider, "GEMINI_DIM", 2)
    client = FakeClient([response(200, json={
        "embeddings": [{"values": [3, 4, 99]}],
        "usageMetadata": {"totalTokenCount": 7},
    })])

    result = provider.solicitar_embeddings_gemini(client, ["texto"], "test-key")

    assert result == [[0.6, 0.8]]
    assert EMBED_STATS == {"requests": 1, "texts": 1, "chars": 5, "tokens_api": 7}
    url, call = client.calls[0]
    assert url == provider.GEMINI_EMBED_URL
    assert call["headers"]["x-goog-api-key"] == "test-key"
    assert call["timeout"] == 120.0
    assert call["json"] == {"requests": [{
        "model": f"models/{provider.GEMINI_EMBED_MODEL}",
        "content": {"parts": [{"text": "texto"}]},
        "taskType": "RETRIEVAL_DOCUMENT",
        "outputDimensionality": 2,
    }]}


def test_429_fail_fast_detiene_sin_esperar() -> None:
    sleeps: list[float] = []
    client = FakeClient([response(429, json={"error": "quota"})])

    with pytest.raises(provider.QuotaExceeded, match="429"):
        provider.solicitar_embeddings_gemini(
            client,
            ["texto"],
            "test-key",
            fail_fast=True,
            sleep=sleeps.append,
        )

    assert len(client.calls) == 1
    assert sleeps == []
    assert EMBED_STATS["requests"] == 0


def test_429_respeta_retry_after_y_backoff(monkeypatch) -> None:
    monkeypatch.setattr(provider, "GEMINI_DIM", 2)
    monkeypatch.setattr(provider, "GEMINI_BACKOFF", (2.0,))
    sleeps: list[float] = []
    client = FakeClient([
        response(429, json={"error": "quota"}, headers={"Retry-After": "130"}),
        response(200, json={"embeddings": [{"values": [1, 0]}]}),
    ])

    result = provider.solicitar_embeddings_gemini(
        client,
        ["texto"],
        "test-key",
        sleep=sleeps.append,
    )

    assert result == [[1.0, 0.0]]
    assert len(client.calls) == 2
    assert sleeps == [120.0, 2.0]


def test_error_de_dimension_se_valida_sin_reintento_en_fail_fast(monkeypatch) -> None:
    monkeypatch.setattr(provider, "GEMINI_DIM", 2)
    sleeps: list[float] = []
    client = FakeClient([response(200, json={"embeddings": [{"values": [1]}]})])

    with pytest.raises(RuntimeError, match="dimensión 1 != 2"):
        provider.solicitar_embeddings_gemini(
            client,
            ["texto"],
            "test-key",
            fail_fast=True,
            sleep=sleeps.append,
        )

    assert len(client.calls) == 1
    assert sleeps == []


def test_error_503_se_reintenta(monkeypatch) -> None:
    monkeypatch.setattr(provider, "GEMINI_DIM", 2)
    monkeypatch.setattr(provider, "GEMINI_BACKOFF", (2.0,))
    sleeps: list[float] = []
    client = FakeClient([
        response(503, json={"error": "unavailable"}),
        response(200, json={"embeddings": [{"values": [0, 1]}]}),
    ])

    result = provider.solicitar_embeddings_gemini(
        client,
        ["texto"],
        "test-key",
        sleep=sleeps.append,
    )

    assert result == [[0.0, 1.0]]
    assert len(client.calls) == 2
    assert sleeps == [2.0]


def test_auth_check_usa_endpoint_independiente_y_no_parsea_body() -> None:
    client = FakeClient([response(403, json={"detail": "ignored"})])

    assert provider.consultar_auth_gemini(client, "test-key") == 403

    url, call = client.calls[0]
    assert url == provider.GEMINI_AUTH_URL
    assert call["timeout"] == 30.0
    assert call["headers"]["x-goog-api-key"] == "test-key"
    assert call["json"]["content"] == {"parts": [{"text": "ok"}]}


def test_entrypoint_conserva_api_y_pasa_la_clave(monkeypatch) -> None:
    captured: dict = {}

    def fake_request(client, texts, api_key, fail_fast=False):
        captured.update({
            "client": client,
            "texts": texts,
            "api_key": api_key,
            "fail_fast": fail_fast,
        })
        return [[1.0]]

    sentinel = object()
    monkeypatch.setattr(entrypoint, "GEMINI_API_KEY", "runtime-key")
    monkeypatch.setattr(entrypoint, "solicitar_embeddings_gemini", fake_request)

    assert entrypoint.embed_lote_gemini(sentinel, ["texto"], fail_fast=True) == [[1.0]]
    assert captured == {
        "client": sentinel,
        "texts": ["texto"],
        "api_key": "runtime-key",
        "fail_fast": True,
    }
    assert entrypoint.QuotaExceeded is provider.QuotaExceeded
    assert entrypoint.GEMINI_EMBED_MODEL == provider.GEMINI_EMBED_MODEL


@pytest.mark.parametrize(
    "api_key, status, expected, marker",
    [
        ("", None, 2, "HTTP=missing auth_ok=false"),
        ("runtime-key", 403, 1, "HTTP=403 auth_fail=True auth_ok=False"),
        ("runtime-key", 200, 0, "HTTP=200 auth_fail=False auth_ok=True"),
    ],
)
def test_entrypoint_auth_check_clasifica_estado_sin_mostrar_body(
    monkeypatch,
    capsys,
    api_key: str,
    status: int | None,
    expected: int,
    marker: str,
) -> None:
    calls: list[tuple[object, str]] = []
    monkeypatch.setattr(entrypoint, "GEMINI_API_KEY", api_key)
    monkeypatch.setattr(
        entrypoint,
        "consultar_auth_gemini",
        lambda client, key: calls.append((client, key)) or status,
    )

    assert entrypoint.auth_check_gemini() == expected
    assert marker in capsys.readouterr().out
    assert calls == ([] if not api_key else [(entrypoint.httpx, api_key)])
