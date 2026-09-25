"""Contrato del transporte Gemini OCR sin consumir servicios reales."""

from __future__ import annotations

import base64
from collections.abc import Iterator

import httpx
import pytest

import descargar_requerimiento as entrypoint
from seace_monitor.ocr import gemini_provider as provider


class FakeClient:
    def __init__(self, responses: list[httpx.Response]):
        self.responses: Iterator[httpx.Response] = iter(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs) -> httpx.Response:
        self.calls.append((url, kwargs))
        return next(self.responses)


def response(status: int, *, json: dict | None = None) -> httpx.Response:
    request = httpx.Request("POST", "https://gemini.test/ocr")
    return httpx.Response(status, json=json, request=request)


@pytest.fixture(autouse=True)
def clean_usage() -> Iterator[None]:
    provider.LAST_OCR_USAGE.clear()
    provider.OCR_USAGE_ACUM.update({
        "prompt": 0,
        "candidates": 0,
        "total": 0,
        "llamadas": 0,
    })
    yield
    provider.LAST_OCR_USAGE.clear()
    provider.OCR_USAGE_ACUM.update({
        "prompt": 0,
        "candidates": 0,
        "total": 0,
        "llamadas": 0,
    })


def test_ocr_envia_imagen_parsea_texto_y_registra_uso() -> None:
    client = FakeClient([response(200, json={
        "candidates": [{"content": {"parts": [
            {"text": "primera"},
            {"text": "razonamiento", "thought": True},
            {"text": "segunda"},
        ]}}],
        "usageMetadata": {
            "promptTokenCount": 11,
            "candidatesTokenCount": 7,
            "totalTokenCount": 18,
        },
    })])

    text = provider.solicitar_ocr_gemini(
        client,
        b"\x00\x01",
        "image/png",
        "test-key",
    )

    assert text == "primera\nsegunda"
    assert provider.LAST_OCR_USAGE == {"prompt": 11, "candidates": 7, "total": 18}
    assert provider.OCR_USAGE_ACUM == {
        "prompt": 11,
        "candidates": 7,
        "total": 18,
        "llamadas": 1,
    }
    url, call = client.calls[0]
    assert url == provider.GEMINI_OCR_URL
    assert call["headers"]["x-goog-api-key"] == "test-key"
    assert call["timeout"] == 120.0
    inline = call["json"]["contents"][0]["parts"][1]["inlineData"]
    assert inline == {
        "mimeType": "image/png",
        "data": base64.b64encode(b"\x00\x01").decode("ascii"),
    }
    assert call["json"]["generationConfig"] == {
        "thinkingConfig": {"thinkingLevel": "LOW"},
        "maxOutputTokens": 4096,
        "temperature": 0.1,
    }


def test_429_detiene_sin_reintentar() -> None:
    sleeps: list[float] = []
    client = FakeClient([response(429, json={"error": "quota"})])

    with pytest.raises(provider.CupoFlash, match="429 OCR") as caught:
        provider.solicitar_ocr_gemini(
            client,
            b"image",
            "image/jpeg",
            "test-key",
            sleep=sleeps.append,
        )

    assert caught.value.motivo == "429"
    assert len(client.calls) == 1
    assert sleeps == []
    assert provider.OCR_USAGE_ACUM["llamadas"] == 0


def test_error_transitorio_reintenta_con_backoff() -> None:
    sleeps: list[float] = []
    client = FakeClient([
        response(503, json={"error": "unavailable"}),
        response(200, json={"candidates": [{"content": {"parts": [
            {"text": "recuperado"},
        ]}}]}),
    ])

    assert provider.solicitar_ocr_gemini(
        client,
        b"image",
        "image/jpeg",
        "test-key",
        sleep=sleeps.append,
    ) == "recuperado"

    assert len(client.calls) == 2
    assert sleeps == [2.0]


def test_errores_agotan_los_cuatro_intentos() -> None:
    sleeps: list[float] = []
    client = FakeClient([
        response(500, json={"error": "failure"}),
        response(500, json={"error": "failure"}),
        response(500, json={"error": "failure"}),
        response(500, json={"error": "failure"}),
    ])

    with pytest.raises(RuntimeError, match="OCR Gemini fallo"):
        provider.solicitar_ocr_gemini(
            client,
            b"image",
            "image/jpeg",
            "test-key",
            sleep=sleeps.append,
        )

    assert len(client.calls) == 4
    assert sleeps == [2.0, 8.0, 20.0]
    assert provider.OCR_USAGE_ACUM["llamadas"] == 0


def test_entrypoint_exige_clave_antes_de_aplicar_rpm(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(entrypoint, "GEMINI_API_KEY", "")
    monkeypatch.setattr(entrypoint, "respetar_rpm", lambda: calls.append("rpm"))

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY ausente"):
        entrypoint.ocr_pagina_gemini(b"image")

    assert calls == []


def test_entrypoint_aplica_rpm_limpia_texto_y_conserva_reexportaciones(monkeypatch) -> None:
    calls: list[tuple] = []

    def fake_ocr(client, image, mime, api_key):
        calls.append((client, image, mime, api_key))
        return "primera  \n\n\n segunda\x00"

    monkeypatch.setattr(entrypoint, "GEMINI_API_KEY", "runtime-key")
    monkeypatch.setattr(entrypoint, "respetar_rpm", lambda: calls.append(("rpm",)))
    monkeypatch.setattr(entrypoint, "solicitar_ocr_gemini", fake_ocr)

    assert entrypoint.ocr_pagina_gemini(b"image", "image/png") == "primera\n\n segunda"
    assert calls == [
        ("rpm",),
        (entrypoint.httpx, b"image", "image/png", "runtime-key"),
    ]
    assert entrypoint.CupoFlash is provider.CupoFlash
    assert entrypoint.LAST_OCR_USAGE is provider.LAST_OCR_USAGE
    assert entrypoint.OCR_USAGE_ACUM is provider.OCR_USAGE_ACUM
    assert entrypoint.GEMINI_FLASH == provider.GEMINI_FLASH
