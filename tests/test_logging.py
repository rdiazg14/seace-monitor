from __future__ import annotations

from seace_monitor.logging import (
    PASO_CHUNKING,
    PASO_EMBEDDING,
    PASO_INGESTA,
    _jsonable,
    registrar_evento,
    registrar_run,
)


class _FallaInsert:
    """Fake de cliente que lanza al hacer .insert(...).execute()."""

    def table(self, _name):
        return self

    def insert(self, _fila):
        return self

    def execute(self):
        raise RuntimeError("boom")


def test_registrar_run_sin_cliente_no_lanza():
    # None => retorno temprano, no debe lanzar.
    assert registrar_run(None, PASO_INGESTA, {}) is None


def test_registrar_run_fail_soft():
    # Un insert que falla no rompe al caller (fail-soft).
    assert registrar_run(_FallaInsert(), PASO_INGESTA, {"k": "v"}) is None


def test_registrar_evento_sin_cliente_no_lanza():
    assert registrar_evento(None, 123, "chunking") is None


def test_registrar_evento_fail_soft():
    assert (
        registrar_evento(
            _FallaInsert(),
            123,
            "chunking",
            n_chunks_pdf=3,
            costo_usd=0.001,
        )
        is None
    )


def test_jsonable_sanea_datetime():
    from datetime import datetime, timezone

    out = _jsonable({"ts": datetime(2026, 1, 1, tzinfo=timezone.utc)})
    assert isinstance(out["ts"], str)


def test_pasos_constantes():
    assert PASO_INGESTA == "ingesta"
    assert PASO_EMBEDDING == "embedding"
    assert PASO_CHUNKING == "chunking"
