"""Tests de seace_monitor.config (carga de `.env` y getters). Puros, sin red ni BD."""
from __future__ import annotations

import os
from pathlib import Path

from seace_monitor.config import cargar_env, obtener, obtener_strip


def _escribir_env(tmp_path: Path, contenido: str) -> Path:
    env = tmp_path / ".env"
    env.write_text(contenido, encoding="utf-8")
    return env


def test_carga_valores_basicos(tmp_path, monkeypatch):
    _escribir_env(
        tmp_path,
        "SUPABASE_URL=https://x.supabase.co\nSUPABASE_SERVICE_KEY=abc\n",
    )
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    cargar_env(root=tmp_path)
    assert os.environ["SUPABASE_URL"] == "https://x.supabase.co"
    assert os.environ["SUPABASE_SERVICE_KEY"] == "abc"


def test_ignora_comentarios_y_lineas_sin_igual(tmp_path, monkeypatch):
    _escribir_env(tmp_path, "# comentario\n\nSIN_IGUAL\nCON_IGUAL=1\n")
    monkeypatch.delenv("CON_IGUAL", raising=False)
    cargar_env(root=tmp_path)
    assert os.environ.get("CON_IGUAL") == "1"
    assert "SIN_IGUAL" not in os.environ


def test_no_pisa_variable_ya_en_entorno(tmp_path, monkeypatch):
    _escribir_env(tmp_path, "CLAVE=del_env\n")
    monkeypatch.setenv("CLAVE", "ya_en_entorno")
    cargar_env(root=tmp_path)
    assert os.environ["CLAVE"] == "ya_en_entorno"


def test_archivo_inexistente_no_falla(tmp_path):
    cargar_env(root=tmp_path / "no_existe")  # no debe lanzar


def test_obtener_y_strip(tmp_path, monkeypatch):
    monkeypatch.setenv("ESPACIOS", "  hola  ")
    assert obtener_strip("ESPACIOS") == "hola"
    assert obtener("NO_EXISTE", "dft") == "dft"
    assert obtener("NO_EXISTE") == ""
