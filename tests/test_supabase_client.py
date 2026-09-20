"""Tests de seace_monitor.supabase_client (sin red: solo el guard de credenciales)."""
from __future__ import annotations

import pytest

from seace_monitor.supabase_client import crear_cliente


def test_crear_cliente_falla_sin_creds(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        crear_cliente()
    assert "SUPABASE_URL" in str(exc.value)
