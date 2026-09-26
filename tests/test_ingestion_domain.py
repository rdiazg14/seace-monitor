"""Validación, transformación y persistencia de ingesta sin servicios reales."""

from __future__ import annotations

from types import SimpleNamespace

from seace_monitor.ingestion.models import filtrar_validos, payload_solo_datos
from seace_monitor.ingestion.repository import registrar_rechazo, upsert_lote
from seace_monitor.ingestion.service import preparar_fila_db


def test_validacion_rechaza_ids_invalidos_sin_perder_el_payload() -> None:
    rejected: list[tuple[dict, str]] = []
    valid = {"idContrato": 10, "desContratacion": "Servicio"}

    accepted, count = filtrar_validos(
        [valid, {"idContrato": 0}, "basura"],
        lambda payload, reason: rejected.append((payload, reason)),
    )

    assert accepted == [valid]
    assert count == 2
    assert rejected[0][0] == {"idContrato": 0}
    assert rejected[1][0] == {"_raw": "basura"}


def test_payload_rechazado_elimina_contexto_de_sesion() -> None:
    cleaned = payload_solo_datos({
        "idContrato": 10,
        "descripcion": "dato",
        "Authorization": "Bearer secreto",
        "cookies": "sesion",
        "x-csrf-token": "token",
        "otro_token_interno": "secreto",
    })

    assert cleaned == {"idContrato": 10, "descripcion": "dato"}


def test_transformacion_conserva_solo_hechos_y_no_clasificacion() -> None:
    row = preparar_fila_db({
        "idContrato": 10,
        "nroContratacion": 99,
        "desContratacion": "Servicio de software",
        "nomObjetoContrato": "Servicios",
        "desObjetoContrato": "Implementación",
        "nomEntidad": "Entidad",
        "nomEstadoContrato": "Vigente",
        "fecPublica": "25/09/2026",
        "fecIniCotizacion": None,
        "fecFinCotizacion": None,
        "idTipoCotizacion": 1,
        "cotizar": True,
        "categoria_it": "NO DEBE PASAR",
    })

    assert row["id"] == 10
    assert row["nro_contratacion"] == "99"
    assert row["cotizar"] is True
    assert "categoria_it" not in row
    assert "relevancia_ia" not in row


class RejectQuery:
    def __init__(self, client):
        self.client = client

    def insert(self, row):
        self.client.inserted.append(row)
        return self

    def execute(self):
        return SimpleNamespace(data=[])


class RejectSupabase:
    def __init__(self):
        self.inserted: list[dict] = []

    def table(self, name):
        assert name == "ingesta_rechazados"
        return RejectQuery(self)


def test_repositorio_de_rechazos_no_persiste_secretos() -> None:
    supa = RejectSupabase()

    registrar_rechazo(supa, {
        "idContrato": 10,
        "descripcion": "dato",
        "Authorization": "Bearer secreto",
    }, "inválido")

    assert supa.inserted[0]["id_contrato"] == 10
    assert supa.inserted[0]["payload"] == {"idContrato": 10, "descripcion": "dato"}


class RetryQuery:
    def __init__(self, client):
        self.client = client

    def upsert(self, rows, on_conflict=None):
        self.client.calls += 1
        return self

    def execute(self):
        if self.client.calls == 1:
            raise RuntimeError("transitorio")
        return SimpleNamespace(data=[])


class RetrySupabase:
    def __init__(self):
        self.calls = 0

    def table(self, name):
        return RetryQuery(self)


def test_upsert_reintenta_sin_duplicar_transformacion() -> None:
    supa = RetrySupabase()
    waits: list[float] = []

    upsert_lote(supa, [{"id": 10}], sleep=waits.append)

    assert supa.calls == 2
    assert waits == [2]
