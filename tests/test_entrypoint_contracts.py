"""Contratos de composición entre el paquete y los CLI históricos."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from seace_monitor.documents import postprocess


ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("script", "expected_option"),
    [
        ("chunker_contratos.py", "--solo-pdf"),
        ("generar_embeddings.py", "--auth-check"),
        ("descargar_requerimiento.py", "--solo-ocr"),
        ("extraer_contenedores.py", "--dry-run"),
    ],
)
def test_cli_historico_conserva_help(script: str, expected_option: str) -> None:
    result = subprocess.run(
        [sys.executable, script, "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert expected_option in result.stdout


def test_paquete_no_importa_entrypoints_raiz() -> None:
    code = """
import sys
from seace_monitor.documents import postprocess
from seace_monitor.embeddings import service as embedding_service
from seace_monitor.rag import service as rag_service

for name in ('chunker_contratos', 'generar_embeddings', 'descargar_requerimiento'):
    assert name not in sys.modules, name
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_postproceso_encadena_pdf_y_embeddings_con_clave_inyectada(
    monkeypatch,
) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        postprocess,
        "run_solo_pdf",
        lambda supa, ids, limit: calls.append(("chunk", supa, ids, limit)),
    )
    monkeypatch.setattr(
        postprocess,
        "run_gemini",
        lambda supa, limit, **kwargs: calls.append(
            ("embed", supa, limit, kwargs)
        ),
    )
    supa = object()

    postprocess.rechunk_embed_pdf(supa, 42, api_key="test-key")

    assert calls == [
        ("chunk", supa, [42], 0),
        (
            "embed",
            supa,
            0,
            {
                "fuente": "pdf",
                "ids": [42],
                "embed_mode": "auto",
                "api_key": "test-key",
            },
        ),
    ]
