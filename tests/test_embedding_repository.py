"""Caracterización de la persistencia de embeddings sin Supabase real."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Callable

import pytest

import generar_embeddings as entrypoint
from seace_monitor.embeddings import repository


class FakeQuery:
    def __init__(self, client: "FakeSupabase", table: str):
        self.client = client
        self.table_name = table
        self.operations: list[tuple] = []

    def _record(self, name: str, *args, **kwargs):
        self.operations.append((name, args, kwargs))
        return self

    @property
    def not_(self):
        return self._record("not")

    def select(self, *args, **kwargs):
        return self._record("select", *args, **kwargs)

    def update(self, *args, **kwargs):
        return self._record("update", *args, **kwargs)

    def upsert(self, *args, **kwargs):
        return self._record("upsert", *args, **kwargs)

    def eq(self, *args, **kwargs):
        return self._record("eq", *args, **kwargs)

    def in_(self, *args, **kwargs):
        return self._record("in", *args, **kwargs)

    def is_(self, *args, **kwargs):
        return self._record("is", *args, **kwargs)

    def order(self, *args, **kwargs):
        return self._record("order", *args, **kwargs)

    def range(self, *args, **kwargs):
        return self._record("range", *args, **kwargs)

    def limit(self, *args, **kwargs):
        return self._record("limit", *args, **kwargs)

    def execute(self):
        self.client.executed.append(self)
        return self.client.handler(self)


class FakeSupabase:
    def __init__(self, handler: Callable[[FakeQuery], SimpleNamespace]):
        self.handler = handler
        self.executed: list[FakeQuery] = []

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)


def operation(query: FakeQuery, name: str) -> tuple:
    return next(item for item in query.operations if item[0] == name)


def response(data=None, count=None) -> SimpleNamespace:
    return SimpleNamespace(data=data, count=count)


@pytest.mark.parametrize("ids, fuente", [([], "pdf"), ([1], "")])
def test_reset_exige_muestra_acotada(ids: list[int], fuente: str) -> None:
    client = FakeSupabase(lambda query: response([]))

    with pytest.raises(SystemExit, match="--ids y --fuente"):
        repository.reset_embedding_v2(client, ids, fuente)

    assert client.executed == []


def test_reset_actualiza_en_lotes_y_filtra_fuente() -> None:
    def handler(query: FakeQuery) -> SimpleNamespace:
        lote = operation(query, "in")[1][1]
        return response([{"id": item} for item in lote])

    client = FakeSupabase(handler)
    ids = list(range(1, 83))

    assert repository.reset_embedding_v2(client, ids, "pdf") == 82
    assert len(client.executed) == 2
    assert operation(client.executed[0], "in")[1] == ("contrato_id", ids[:80])
    assert operation(client.executed[1], "in")[1] == ("contrato_id", ids[80:])
    for query in client.executed:
        assert operation(query, "update")[1] == ({"embedding_v2": None},)
        assert operation(query, "eq")[1] == ("fuente", "pdf")


def test_chunks_pendientes_respetan_null_fuente_limite_y_paginacion(monkeypatch) -> None:
    monkeypatch.setattr(repository, "PAGE", 2)

    def handler(query: FakeQuery) -> SimpleNamespace:
        start, _ = operation(query, "range")[1]
        rows = [{"id": 1}, {"id": 2}] if start == 0 else [{"id": 3}]
        return response(rows)

    client = FakeSupabase(handler)

    rows = repository.chunks_sin_embedding_v2(client, [10, 11], 3, fuente="pdf")

    assert rows == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert [operation(query, "range")[1] for query in client.executed] == [
        (0, 1),
        (2, 2),
    ]
    for query in client.executed:
        assert operation(query, "in")[1] == ("contrato_id", [10, 11])
        assert operation(query, "is")[1] == ("embedding_v2", "null")
        assert operation(query, "eq")[1] == ("fuente", "pdf")


def test_guardar_embeddings_hace_un_upsert_con_formato_vectorial() -> None:
    client = FakeSupabase(lambda query: response([]))
    rows = [{
        "id": 7,
        "contrato_id": 20,
        "chunk_index": 1,
        "tipo": "requisito",
        "texto": "Experiencia mínima",
    }]

    repository.guardar_embeddings_v2(client, rows, [[1.0, -0.123456789]])

    assert len(client.executed) == 1
    assert operation(client.executed[0], "upsert")[1] == ([{
        **rows[0],
        "embedding_v2": "[1.00000000,-0.12345679]",
    }],)
    assert operation(client.executed[0], "upsert")[2] == {"on_conflict": "id"}


def test_cobertura_agrega_conteos_por_lotes(monkeypatch) -> None:
    ids = list(range(1, 82))
    monkeypatch.setattr(repository, "paginar_ids_vigentes", lambda supa: ids)
    counts = iter([100, 75, 4, 3])
    client = FakeSupabase(lambda query: response([], next(counts)))

    result = repository.cobertura_vigentes(client)

    assert result == {
        "vigentes": 81,
        "chunks_vigentes": 104,
        "chunks_v2": 78,
        "chunks_v2_null": 26,
    }
    assert len(client.executed) == 4
    assert operation(client.executed[0], "in")[1][1] == ids[:80]
    assert operation(client.executed[2], "in")[1][1] == ids[80:]
    assert all(
        any(item[0] == "not" for item in client.executed[index].operations)
        for index in (1, 3)
    )


def test_contar_embeddings_aplica_fuente_y_normaliza_count_none() -> None:
    client = FakeSupabase(lambda query: response([], None))

    assert repository.contar_embeddings_v2(client, [1, 2], "api") == 0
    query = client.executed[0]
    assert operation(query, "in")[1] == ("contrato_id", [1, 2])
    assert operation(query, "eq")[1] == ("fuente", "api")
    assert operation(query, "is")[1] == ("embedding_v2", "null")
    assert any(item[0] == "not" for item in query.operations)


def test_entrypoint_conserva_reexportaciones_de_persistencia() -> None:
    assert entrypoint.PAGE == repository.PAGE
    assert entrypoint.reset_embedding_v2 is repository.reset_embedding_v2
    assert entrypoint.paginar_ids_vigentes is repository.paginar_ids_vigentes
    assert entrypoint.chunks_sin_embedding_v2 is repository.chunks_sin_embedding_v2
    assert entrypoint.chunks_sin_v2_por_fuente is repository.chunks_sin_v2_por_fuente
    assert entrypoint.guardar_embeddings_v2 is repository.guardar_embeddings_v2
    assert entrypoint.cobertura_vigentes is repository.cobertura_vigentes
