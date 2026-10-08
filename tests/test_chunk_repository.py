"""Pruebas del límite de persistencia del chunker sin Supabase real."""

from __future__ import annotations

import pytest

from types import SimpleNamespace
from typing import Callable

import chunker_contratos as entrypoint
from seace_monitor.rag import repository, service


class FakeNot:
    def __init__(self, query: "FakeQuery"):
        self.query = query

    def in_(self, *args, **kwargs):
        return self.query._record("not_in", *args, **kwargs)


class FakeQuery:
    def __init__(self, client: "FakeSupabase", table: str):
        self.client = client
        self.table_name = table
        self.operations: list[tuple] = []

    def _record(self, name: str, *args, **kwargs):
        self.operations.append((name, args, kwargs))
        return self

    def select(self, *args, **kwargs):
        return self._record("select", *args, **kwargs)

    def eq(self, *args, **kwargs):
        return self._record("eq", *args, **kwargs)

    def neq(self, *args, **kwargs):
        return self._record("neq", *args, **kwargs)

    @property
    def not_(self) -> FakeNot:
        return FakeNot(self)

    def in_(self, *args, **kwargs):
        return self._record("in", *args, **kwargs)

    def order(self, *args, **kwargs):
        return self._record("order", *args, **kwargs)

    def range(self, *args, **kwargs):
        return self._record("range", *args, **kwargs)

    def upsert(self, *args, **kwargs):
        return self._record("upsert", *args, **kwargs)

    def delete(self, *args, **kwargs):
        return self._record("delete", *args, **kwargs)

    def update(self, *args, **kwargs):
        return self._record("update", *args, **kwargs)

    def limit(self, *args, **kwargs):
        return self._record("limit", *args, **kwargs)

    def execute(self):
        self.client.executed.append(self)
        return self.client.handler(self)


class FakeSupabase:
    def __init__(self, handler: Callable[[FakeQuery], SimpleNamespace]):
        self.handler = handler
        self.created: list[FakeQuery] = []
        self.executed: list[FakeQuery] = []

    def table(self, name: str) -> FakeQuery:
        query = FakeQuery(self, name)
        self.created.append(query)
        return query


def operation(query: FakeQuery, name: str) -> tuple:
    return next(item for item in query.operations if item[0] == name)


def response(data=None, count=None) -> SimpleNamespace:
    return SimpleNamespace(data=data, count=count)


def test_paginar_conserva_filtros_y_orden_estable(monkeypatch) -> None:
    monkeypatch.setattr(repository, "PAGE", 2)

    def handler(query: FakeQuery) -> SimpleNamespace:
        start, _ = operation(query, "range")[1]
        return response([{"id": 1}, {"id": 2}] if start == 0 else [{"id": 3}])

    client = FakeSupabase(handler)
    rows = repository.paginar(
        client,
        "contratos",
        "id",
        eq={"estado": "Vigente", "detalle_cargado": True},
    )

    assert rows == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert len(client.executed) == 2
    assert [operation(query, "range")[1] for query in client.executed] == [(0, 1), (2, 3)]
    for query in client.executed:
        assert operation(query, "order")[1] == ("id",)
        assert [item[1] for item in query.operations if item[0] == "eq"] == [
            ("estado", "Vigente"),
            ("detalle_cargado", True),
        ]


def test_insert_lote_reintenta_sin_columnas_nuevas_para_esquema_antiguo() -> None:
    attempts = 0

    def handler(query: FakeQuery) -> SimpleNamespace:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("PGRST204: chunk_embed_text column missing")
        return response([])

    client = FakeSupabase(handler)
    repository.insert_lote(client, [{
        "contrato_id": 42,
        "chunk_index": 0,
        "texto": "contenido",
        "meta_entidad": "Entidad",
        "meta_nro": "001",
        "chunk_embed_text": "contenido",
    }])

    assert attempts == 2
    retry_rows = operation(client.executed[1], "upsert")[1][0]
    assert retry_rows == [{
        "contrato_id": 42,
        "chunk_index": 0,
        "texto": "contenido",
    }]
    assert operation(client.executed[1], "upsert")[2] == {
        "on_conflict": "contrato_id,chunk_index",
    }


def test_borrar_por_fuente_no_toca_otras_fuentes_y_respeta_lotes() -> None:
    client = FakeSupabase(lambda query: response([]))
    ids = list(range(1, 83))

    repository.borrar_chunks_fuente(client, ids, "pdf")

    assert len(client.executed) == 2
    assert operation(client.executed[0], "in")[1] == ("contrato_id", ids[:80])
    assert operation(client.executed[1], "in")[1] == ("contrato_id", ids[80:])
    assert all(operation(query, "eq")[1] == ("fuente", "pdf") for query in client.executed)


def test_max_chunk_index_cubre_contrato_nuevo_y_existente() -> None:
    responses = iter([response([]), response([{"chunk_index": 7}])])
    client = FakeSupabase(lambda query: next(responses))

    assert repository.max_chunk_index(client, 10) == -1
    assert repository.max_chunk_index(client, 11) == 7
    assert [operation(query, "eq")[1] for query in client.executed] == [
        ("contrato_id", 10),
        ("contrato_id", 11),
    ]


def test_max_chunk_index_puede_excluir_una_fuente() -> None:
    client = FakeSupabase(lambda query: response([{"chunk_index": 3}]))

    assert repository.max_chunk_index(client, 9, excluir_fuente="pdf") == 3
    assert operation(client.executed[0], "neq")[1] == ("fuente", "pdf")


def test_reemplazar_chunks_contrato_no_toca_corpus_si_no_hay_chunks() -> None:
    client = FakeSupabase(lambda query: response([]))

    assert repository.reemplazar_chunks_contrato(client, 5, [], fuente="pdf") == 0
    assert client.executed == []


def test_reemplazar_hace_upsert_antes_de_limpiar_restos() -> None:
    client = FakeSupabase(lambda query: response([]))
    chunks = [
        {"contrato_id": 7, "chunk_index": 4, "texto": "a"},
        {"contrato_id": 7, "chunk_index": 5, "texto": "b"},
    ]

    assert repository.reemplazar_chunks_contrato(client, 7, chunks, fuente="pdf") == 2
    assert len(client.executed) == 2
    upsert_query, delete_query = client.executed
    assert operation(upsert_query, "upsert")[1][0] == chunks
    assert operation(delete_query, "eq")[1] == ("contrato_id", 7)
    eqs = [item[1] for item in delete_query.operations if item[0] == "eq"]
    assert ("fuente", "pdf") in eqs
    assert operation(delete_query, "not_in")[1] == ("chunk_index", [4, 5])


def test_reemplazar_sin_fuente_limpia_todas_las_fuentes() -> None:
    client = FakeSupabase(lambda query: response([]))

    repository.reemplazar_chunks_contrato(
        client, 7, [{"contrato_id": 7, "chunk_index": 0, "texto": "a"}]
    )

    delete_query = client.executed[1]
    eqs = [item[1] for item in delete_query.operations if item[0] == "eq"]
    assert eqs == [("contrato_id", 7)]


def test_reemplazar_no_borra_si_el_insert_falla() -> None:
    def handler(query: FakeQuery) -> SimpleNamespace:
        if any(item[0] == "upsert" for item in query.operations):
            raise RuntimeError("red caída")
        return response([])

    client = FakeSupabase(handler)
    with pytest.raises(RuntimeError):
        repository.reemplazar_chunks_contrato(
            client, 7, [{"contrato_id": 7, "chunk_index": 0, "texto": "a"}]
        )

    assert not any(
        item[0] == "delete"
        for query in client.executed
        for item in query.operations
    )


def test_run_solo_pdf_marca_evento_solo_tras_escritura_real(monkeypatch) -> None:
    contrato = {"id": 7, "tdr_texto": "contenido tdr", "chunk_version": "300_60"}
    eventos: list[tuple] = []

    def handler(query: FakeQuery) -> SimpleNamespace:
        if query.table_name == "contratos":
            return response([contrato])
        return response([])

    monkeypatch.setattr(
        service, "chunks_de_pdf",
        lambda c, chunk_index_offset: [
            {"contrato_id": 7, "chunk_index": chunk_index_offset, "texto": "x"}
        ],
    )
    monkeypatch.setattr(
        service, "registrar_evento",
        lambda supa, cid, evento, **kw: eventos.append((cid, evento)),
    )
    monkeypatch.setattr(service, "registrar_run", lambda *a, **kw: None)

    client = FakeSupabase(handler)
    service.run_solo_pdf(client, [7], 0)

    assert eventos == [(7, "chunked")]
    writes = [
        query for query in client.executed
        if any(item[0] == "upsert" for item in query.operations)
    ]
    assert writes, "el evento se marcó sin escritura previa"


def test_run_solo_pdf_fallo_de_escritura_no_marca_evento(monkeypatch) -> None:
    contrato = {"id": 7, "tdr_texto": "contenido tdr"}
    eventos: list[tuple] = []

    def handler(query: FakeQuery) -> SimpleNamespace:
        if query.table_name == "contratos":
            return response([contrato])
        return response([])

    monkeypatch.setattr(
        service, "chunks_de_pdf",
        lambda c, chunk_index_offset: [{"contrato_id": 7, "chunk_index": 0}],
    )
    monkeypatch.setattr(
        service, "reemplazar_chunks_contrato",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("caída")),
    )
    monkeypatch.setattr(
        service, "registrar_evento",
        lambda supa, cid, evento, **kw: eventos.append((cid, evento)),
    )
    monkeypatch.setattr(service, "registrar_run", lambda *a, **kw: None)

    client = FakeSupabase(handler)
    service.run_solo_pdf(client, [7], 0)

    assert eventos == []


def test_entrypoint_conserva_reexportaciones_de_persistencia() -> None:
    assert entrypoint.paginar is repository.paginar
    assert entrypoint.insert_lote is repository.insert_lote
    assert entrypoint.borrar_chunks_fuente is repository.borrar_chunks_fuente
