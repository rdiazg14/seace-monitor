"""Pruebas del límite de persistencia del chunker sin Supabase real."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Callable

import chunker_contratos as entrypoint
from seace_monitor.rag import repository


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


def test_entrypoint_conserva_reexportaciones_de_persistencia() -> None:
    assert entrypoint.paginar is repository.paginar
    assert entrypoint.insert_lote is repository.insert_lote
    assert entrypoint.borrar_chunks_fuente is repository.borrar_chunks_fuente
