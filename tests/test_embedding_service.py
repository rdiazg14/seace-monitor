"""Orquestacion de embeddings con proveedor y persistencia simulados."""

from __future__ import annotations

import pytest

from seace_monitor.embeddings import service
from seace_monitor.embeddings.preparation import reset_embed_stats


class FakeHttpContext:
    def __init__(self, client: object):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_run_gemini_inyecta_clave_y_persiste_lote(monkeypatch) -> None:
    reset_embed_stats()
    row = {
        "id": 7,
        "contrato_id": 42,
        "chunk_index": 0,
        "tipo": "tdr_pdf",
        "texto": "[ABC | 1]\ncontenido",
        "fuente": "pdf",
        "chunk_embed_text": "contenido",
    }
    captured: dict = {}
    client = object()

    monkeypatch.setattr(
        service,
        "chunks_sin_v2_por_fuente",
        lambda supa, fuente, limit: [row],
    )
    monkeypatch.setattr(
        service,
        "solicitar_embeddings_gemini",
        lambda http, texts, api_key, fail_fast=False: captured.update(
            {
                "http": http,
                "texts": texts,
                "api_key": api_key,
                "fail_fast": fail_fast,
            }
        ) or [[1.0, 0.0]],
    )
    monkeypatch.setattr(
        service,
        "guardar_embeddings_v2",
        lambda supa, rows, vectors: captured.update(
            {"supa": supa, "rows": rows, "vectors": vectors}
        ),
    )
    monkeypatch.setattr(service, "contar_embeddings_v2", lambda *args: 1)
    events: list[tuple] = []
    monkeypatch.setattr(
        service,
        "registrar_evento",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    runs: list[tuple] = []
    monkeypatch.setattr(
        service,
        "registrar_run",
        lambda *args, **kwargs: runs.append((args, kwargs)),
    )
    sleeps: list[float] = []
    supa = object()

    result = service.run_gemini(
        supa,
        0,
        fuente="pdf",
        ids=[42],
        delay=0,
        fail_fast=True,
        api_key="test-key",
        http_client_factory=lambda: FakeHttpContext(client),
        sleep=sleeps.append,
    )

    assert result == {"ok": 1, "err": 0, "total": 1, "pendientes": 0}
    assert captured == {
        "http": client,
        "texts": ["contenido"],
        "api_key": "test-key",
        "fail_fast": True,
        "supa": supa,
        "rows": [row],
        "vectors": [[1.0, 0.0]],
    }
    assert sleeps == [0]
    assert len(events) == 1
    assert len(runs) == 1


def test_run_gemini_rechaza_clave_ausente_antes_de_consultar() -> None:
    with pytest.raises(SystemExit, match="GEMINI_API_KEY"):
        service.run_gemini(object(), 0, api_key="")
