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
        "chunks_sin_por_fuente",
        lambda supa, col, fuente, limit: [row],
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
        "guardar_embeddings",
        lambda supa, col, rows, vectors: captured.update(
            {"supa": supa, "col": col, "rows": rows, "vectors": vectors}
        ),
    )
    monkeypatch.setattr(service, "contar_embeddings", lambda *args: 1)
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
        "col": "embedding_v2",
        "rows": [row],
        "vectors": [[1.0, 0.0]],
    }
    assert sleeps == [0]
    assert len(events) == 1
    assert len(runs) == 1


def test_run_gemini_rechaza_clave_ausente_antes_de_consultar() -> None:
    with pytest.raises(SystemExit, match="GEMINI_API_KEY"):
        service.run_gemini(object(), 0, api_key="")


class FakeUsoIa:
    def __init__(self) -> None:
        self.filas: list[dict] = []

    def table(self, nombre: str):
        assert nombre == "uso_ia"
        return self

    def insert(self, fila: dict):
        self.filas.append(fila)
        return self

    def execute(self):
        return None


def test_run_gemini_registra_consumo_si_la_corrida_se_interrumpe(monkeypatch) -> None:
    reset_embed_stats()
    rows = [
        {"id": n, "contrato_id": 42, "chunk_index": n, "tipo": "tdr_pdf",
         "texto": "contenido", "fuente": "pdf", "chunk_embed_text": "contenido"}
        for n in (1, 2)
    ]
    llamadas: list[int] = []

    def solicitar(http, texts, api_key, fail_fast=False):
        llamadas.append(len(texts))
        if len(llamadas) == 2:
            raise service.QuotaExceeded("cupo agotado")
        # El proveedor ya cobro este lote.
        service.EMBED_STATS["tokens_api"] += 1000
        return [[1.0, 0.0]]

    monkeypatch.setattr(service, "chunks_sin_por_fuente", lambda *args: rows)
    monkeypatch.setattr(service, "guardar_embeddings", lambda *args: None)
    eventos: list[dict] = []
    monkeypatch.setattr(
        service, "registrar_evento", lambda *args, **kwargs: eventos.append(kwargs)
    )
    runs: list[dict] = []
    monkeypatch.setattr(
        service, "registrar_run", lambda supa, paso, datos: runs.append(datos)
    )
    supa = FakeUsoIa()

    with pytest.raises(service.QuotaExceeded):
        service.run_gemini(
            supa,
            0,
            fuente="pdf",
            ids=[42],
            batch=1,
            delay=0,
            api_key="test-key",
            http_client_factory=lambda: FakeHttpContext(object()),
            sleep=lambda _: None,
            solicitar=solicitar,
            precio_in=0.07,
            columna="embedding_v3",
        )

    assert len(supa.filas) == 1
    fila = supa.filas[0]
    assert fila["tokens_total"] == 1000
    assert fila["costo_usd"] == 0.00007
    assert fila["detalle"]["resultado"] == "interrumpido"
    assert fila["detalle"]["n_chunks"] == 1
    assert runs[0]["resultado"] == "interrumpido"
    assert len(eventos) == 1


def test_run_gemini_no_oculta_la_interrupcion_si_falla_el_registro(monkeypatch) -> None:
    reset_embed_stats()
    row = {"id": 1, "contrato_id": 42, "chunk_index": 0, "tipo": "tdr_pdf",
           "texto": "contenido", "fuente": "pdf", "chunk_embed_text": "contenido"}

    def solicitar(http, texts, api_key, fail_fast=False):
        service.EMBED_STATS["tokens_api"] += 10
        raise KeyboardInterrupt

    def registrar_run_roto(*args, **kwargs):
        raise RuntimeError("sin conexion")

    monkeypatch.setattr(service, "chunks_sin_por_fuente", lambda *args: [row])
    monkeypatch.setattr(service, "registrar_run", registrar_run_roto)

    with pytest.raises(KeyboardInterrupt):
        service.run_gemini(
            FakeUsoIa(),
            0,
            fuente="pdf",
            ids=[42],
            batch=1,
            delay=0,
            api_key="test-key",
            http_client_factory=lambda: FakeHttpContext(object()),
            sleep=lambda _: None,
            solicitar=solicitar,
            columna="embedding_v3",
        )
