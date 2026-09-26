"""Persistencia documental con dobles de Supabase."""

from __future__ import annotations

from types import SimpleNamespace

from seace_monitor.documents import repository


class FakeQuery:
    def __init__(self, client, payload=None):
        self.client = client
        self.payload = payload

    def select(self, value):
        self.client.selects.append(value)
        return self

    def in_(self, field, values):
        self.client.filters.append(("in", field, list(values)))
        return self

    def eq(self, field, value):
        self.client.filters.append(("eq", field, value))
        return self

    def execute(self):
        if self.payload is not None:
            self.client.updates.append(dict(self.payload))
            if self.client.failures:
                raise self.client.failures.pop(0)
        return SimpleNamespace(data=list(self.client.rows))


class FakeSupabase:
    def __init__(self, rows=None, failures=None):
        self.rows = rows or []
        self.failures = list(failures or [])
        self.tables: list[str] = []
        self.selects: list[str] = []
        self.filters: list[tuple] = []
        self.updates: list[dict] = []

    def table(self, name):
        self.tables.append(name)
        return self

    def select(self, value):
        return FakeQuery(self).select(value)

    def update(self, payload):
        return FakeQuery(self, payload)


def test_contratos_por_ids_restituye_orden_solicitado() -> None:
    supa = FakeSupabase(rows=[{"id": 20}, {"id": 10}])

    result = repository.contratos_por_ids(supa, [10, 20])

    assert [row["id"] for row in result] == [10, 20]
    assert supa.filters == [("in", "id", [10, 20])]


def test_update_contrato_traduce_metadatos_privados_sin_mutar_payload() -> None:
    supa = FakeSupabase()
    payload = {
        "pdf_descargado": True,
        "_pdf_archivo_id": 91,
        "_pdf_nombre": "tdr.pdf",
    }

    repository.update_contrato(supa, 42, payload)

    assert payload["_pdf_archivo_id"] == 91
    assert supa.updates == [{
        "pdf_descargado": True,
        "pdf_archivo_id": 91,
        "pdf_nombre": "tdr.pdf",
    }]
    assert supa.filters == [("eq", "id", 42)]


def test_update_contrato_reintenta_sin_columnas_de_extraccion() -> None:
    supa = FakeSupabase(failures=[Exception("column tdr_tipo_extraccion missing")])

    repository.update_contrato(supa, 42, {
        "pdf_descargado": True,
        "tdr_tipo_extraccion": "mixto",
        "paginas_ocr_pendientes": [2],
        "_pdf_archivo_id": 91,
    })

    assert supa.updates[0]["tdr_tipo_extraccion"] == "mixto"
    assert supa.updates[1] == {
        "pdf_descargado": True,
        "pdf_archivo_id": 91,
    }
