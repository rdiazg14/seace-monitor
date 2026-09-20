"""Tests de seace_monitor.db (solo el guard de credenciales; sin conexión real)."""
from __future__ import annotations

import pytest

from seace_monitor.db import connect


def test_connect_falla_sin_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        connect()
