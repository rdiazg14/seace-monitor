"""Caracterización de la preparación local previa al proveedor de embeddings."""

from __future__ import annotations

import generar_embeddings as entrypoint
from seace_monitor.embeddings import preparation
from seace_monitor.embeddings.preparation import (
    EMBED_STATS,
    MAX_CHARS_GEMINI,
    modo_embed_fila,
    print_embed_stats,
    reset_embed_stats,
    texto_para_embed,
    vec_literal,
)


def test_modo_auto_preserva_header_api_y_usa_body_para_pdf() -> None:
    assert modo_embed_fila({"fuente": "api"}, "auto") == "header"
    assert modo_embed_fila({"fuente": "pdf"}, "auto") == "body"
    assert modo_embed_fila({"fuente": "pdf"}, "header") == "header"
    assert modo_embed_fila({"fuente": "api"}, "body") == "body"


def test_pdf_prefiere_texto_semantico_preparado() -> None:
    row = {
        "fuente": "pdf",
        "texto": "[ABC | 1]\nTexto visible",
        "chunk_embed_text": "  [Entidad | Objeto | 1] Texto limpio  ",
    }
    assert texto_para_embed(row, "auto") == "[Entidad | Objeto | 1] Texto limpio"


def test_body_retira_header_y_header_lo_conserva() -> None:
    row = {"fuente": "api", "texto": "[Entidad | Objeto | 1]\nContenido"}
    assert texto_para_embed(row, "body") == "Contenido"
    assert texto_para_embed(row, "header") == row["texto"]


def test_textos_se_recortan_al_limite_productivo() -> None:
    long_text = "x" * (MAX_CHARS_GEMINI + 50)
    assert texto_para_embed({"fuente": "api", "texto": long_text}, "header") == (
        "x" * MAX_CHARS_GEMINI
    )
    assert texto_para_embed(
        {"fuente": "pdf", "chunk_embed_text": long_text},
        "auto",
    ) == ("x" * MAX_CHARS_GEMINI)


def test_vector_literal_conserva_formato_y_precision() -> None:
    assert vec_literal([1.0, -0.123456789, 0.0]) == (
        "[1.00000000,-0.12345679,0.00000000]"
    )


def test_estadisticas_compartidas_se_reinician_y_reportan(capsys) -> None:
    EMBED_STATS.update({"requests": 2, "texts": 3, "chars": 40, "tokens_api": 9})
    print_embed_stats("  ")
    assert capsys.readouterr().out == (
        "  embed_stats requests=2 texts=3 chars=40 "
        "tokens_est(chars/4)=10 tokens_api=9\n"
    )

    reset_embed_stats()
    assert EMBED_STATS == {"requests": 0, "texts": 0, "chars": 0, "tokens_api": 0}


def test_entrypoint_conserva_reexportaciones() -> None:
    assert entrypoint.EMBED_STATS is preparation.EMBED_STATS
    assert entrypoint.modo_embed_fila is preparation.modo_embed_fila
    assert entrypoint.texto_para_embed is preparation.texto_para_embed
    assert entrypoint.vec_literal is preparation.vec_literal
