"""Clasificación Gemini: reglas, transporte y protección de capas."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import httpx
import pytest

from seace_monitor.classification import repository
from seace_monitor.classification.contracts import RESPONSE_SCHEMA, SYSTEM_PROMPT
from seace_monitor.classification.gemini_provider import clasificar_lote_gemini
from seace_monitor.classification.rules import (
    emparejar_lote,
    parse_array,
    parse_p1_item,
    verificar_senal,
)
from seace_monitor.classification.service import user_prompt, user_prompt_p2


class FakeHttp:
    def __init__(self, responses: list[httpx.Response]):
        self.responses: Iterator[httpx.Response] = iter(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs) -> httpx.Response:
        self.calls.append((url, kwargs))
        return next(self.responses)


def response(status: int, body: dict | None = None, **headers) -> httpx.Response:
    request = httpx.Request("POST", "https://gemini.test/classify")
    return httpx.Response(status, json=body, headers=headers, request=request)


def gemini_body(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def test_parseo_y_emparejado_no_inventan_respuesta_ausente() -> None:
    parsed = parse_array('```json\n{"resultados":[{"id":1,"categoria":"Hardware"}]}\n```')
    matched, missing = emparejar_lote(
        [{"id": 1}, {"id": 2}],
        [*parsed, {"id": 1, "categoria": "Licencias"}, {"id": 99}],
    )

    assert matched == {1: {"id": 1, "categoria": "Hardware"}}
    assert missing == [2]
    assert parse_p1_item({"categoria": "ninguna", "confianza": "baja", "senal": "x"}) == {
        "categoria": "ninguna", "confianza": "alta", "senal": "",
    }


def test_senal_se_verifica_solo_con_texto_del_contrato() -> None:
    row = {
        "descripcion": "Adquisición de licencias de software de diseño",
        "objeto": "Servicio",
        "items_json": [{"descripcion": "Suscripción anual", "nom_cubso": "Software"}],
    }

    assert verificar_senal("licencias de software", row) == (True, "descripcion")
    assert verificar_senal("servidores cloud", row) == (False, "ninguna")


def test_prompts_separan_contexto_completo_y_desempate_ciego() -> None:
    row = {
        "id": 7,
        "descripcion": "Licencias",
        "objeto": "Software",
        "entidad": "Entidad sensible",
        "nom_area_usuaria": "Área TI",
        "items_json": [{"descripcion": "Producto", "nom_cubso": "Catálogo"}],
    }

    assert "Entidad sensible" in user_prompt([row])
    blind = user_prompt_p2([row], pistas="PISTA CONTROLADA")
    assert "Entidad sensible" not in blind
    assert "Área TI" not in blind
    assert "Catálogo" not in blind
    assert "PISTA CONTROLADA" in blind


def test_provider_envia_schema_parsea_y_notifica_uso() -> None:
    client = FakeHttp([response(200, gemini_body('[{"id":1,"categoria":"Hardware"}]'))])
    before: list[str] = []
    success: list[dict] = []

    result = clasificar_lote_gemini(
        client,
        [{"id": 1}],
        system_prompt=SYSTEM_PROMPT,
        schema=RESPONSE_SCHEMA,
        armar_prompt=lambda rows: "prompt",
        api_key="runtime-key",
        url="https://gemini.test/classify",
        parse_response=parse_array,
        before_call=lambda: before.append("quota"),
        on_success=success.append,
        backoff=(),
    )

    assert result == [{"id": 1, "categoria": "Hardware"}]
    assert before == ["quota"]
    assert success == [gemini_body('[{"id":1,"categoria":"Hardware"}]')]
    _, call = client.calls[0]
    assert call["headers"]["x-goog-api-key"] == "runtime-key"
    assert call["json"]["generationConfig"]["responseSchema"] == RESPONSE_SCHEMA


def test_provider_reintenta_transitorio_y_propaga_cupo() -> None:
    waits: list[float] = []
    client = FakeHttp([
        response(503, {"error": "down"}),
        response(200, gemini_body("[]")),
    ])

    assert clasificar_lote_gemini(
        client,
        [],
        system_prompt="system",
        schema={},
        armar_prompt=lambda rows: "prompt",
        api_key="key",
        url="https://gemini.test/classify",
        parse_response=parse_array,
        backoff=(2.0,),
        sleep=waits.append,
    ) == []
    assert waits == [2.0]

    class QuotaError(Exception):
        pass

    with pytest.raises(QuotaError):
        clasificar_lote_gemini(
            FakeHttp([]),
            [],
            system_prompt="system",
            schema={},
            armar_prompt=lambda rows: "prompt",
            api_key="key",
            url="https://gemini.test/classify",
            parse_response=parse_array,
            before_call=lambda: (_ for _ in ()).throw(QuotaError("tope")),
            quota_error_types=(QuotaError,),
            backoff=(),
        )


class FakeQuery:
    def __init__(self, client, mode="select", payload=None):
        self.client = client
        self.mode = mode
        self.payload = payload

    def select(self, value):
        return self

    def in_(self, field, values):
        return self

    def upsert(self, payload, on_conflict=None):
        return FakeQuery(self.client, "upsert", payload)

    def execute(self):
        if self.mode == "upsert":
            self.client.upserts.extend(self.payload)
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=list(self.client.previous))


class FakeSupabase:
    def __init__(self, previous):
        self.previous = previous
        self.upserts: list[dict] = []

    def table(self, name):
        return FakeQuery(self)


def test_repositorio_gemini_no_pisa_humano_y_preserva_relevancia() -> None:
    supa = FakeSupabase([
        {"contrato_id": 1, "capa": "humano", "relevancia_ia": "ALTA"},
        {"contrato_id": 2, "capa": "keyword", "relevancia_ia": "MEDIA"},
    ])

    written, skipped = repository.upsert_gemini_supa(supa, [
        {"contrato_id": 1, "categoria_it": "Hardware"},
        {"contrato_id": 2, "categoria_it": "Licencias", "confianza": 3.0},
    ])

    assert (written, skipped) == (1, 1)
    assert [row["contrato_id"] for row in supa.upserts] == [2]
    assert supa.upserts[0]["relevancia_ia"] == "MEDIA"
    assert supa.upserts[0]["capa"] == "gemini"
