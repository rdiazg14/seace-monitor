"""Caracterización de las transformaciones puras del chunker."""

from __future__ import annotations

import json

import chunker_contratos as entrypoint
from seace_monitor.rag.chunking import (
    approx_tokens,
    chunks_de_contrato,
    chunks_de_pdf,
    cuerpo_chunk,
    cuerpo_sin_membrete,
    embed_text_pdf,
    encabezado_pdf,
    siglas_entidad,
    split_por_parrafos,
)


def contrato_base(**overrides) -> dict:
    contrato = {
        "id": 42,
        "descripcion_contrato": "N.º 001-2026",
        "nro_contratacion": "001-2026",
        "descripcion": "Servicio de analítica",
        "entidad": "Ministerio de Prueba - Centro Nacional de Datos",
        "objeto": "Servicio",
        "estado": "Vigente",
        "nom_area_usuaria": "Tecnología",
        "items_json": [],
        "tdr_texto": "",
    }
    contrato.update(overrides)
    return contrato


def test_estimacion_y_split_preservan_parrafos() -> None:
    assert approx_tokens("") == 0
    assert approx_tokens("abc") == 1
    assert approx_tokens("abcdefgh") == 2
    assert split_por_parrafos("uno\r\n\r\ndos\ntres", 2) == ["uno\ndos", "tres"]
    assert split_por_parrafos("  \n ", 10) == []


def test_header_pdf_y_cuerpo_mantienen_contrato_existente() -> None:
    contrato = contrato_base()
    assert siglas_entidad(contrato["entidad"]) == "CND"
    assert encabezado_pdf(contrato) == "[CND | N.º 001-2026]"
    assert cuerpo_chunk("[CND | N.º 001-2026]\nContenido") == "Contenido"
    assert cuerpo_chunk("Contenido sin header") == "Contenido sin header"
    assert entrypoint.cuerpo_chunk("[X]\ntexto") == "texto"


def test_limpieza_membrete_conserva_secciones_del_tdr() -> None:
    texto = """PERÚ

MINISTERIO DE DEFENSA
OBJETO:
Servicio de soporte


https://www.gob.pe/entidad
REQUISITOS:
Experiencia mínima
"""
    assert cuerpo_sin_membrete(texto) == (
        "OBJETO:\nServicio de soporte\n\nREQUISITOS:\nExperiencia mínima"
    )


def test_chunk_pdf_incluye_offset_metadata_y_texto_de_embedding() -> None:
    contrato = contrato_base(tdr_texto="Contenido contractual")
    chunks = chunks_de_pdf(contrato, chunk_index_offset=3)
    assert chunks == [{
        "contrato_id": 42,
        "chunk_index": 3,
        "tipo": "TDR PDF",
        "texto": "[CND | N.º 001-2026]\nContenido contractual",
        "fuente": "pdf",
        "chunk_embed_text": (
            "[Ministerio de Prueba - Centro Nacional de Datos | "
            "Servicio de analítica | N.º 001-2026] Contenido contractual"
        ),
        "meta_entidad": "Ministerio de Prueba - Centro Nacional de Datos",
        "meta_nro": "N.º 001-2026",
    }]


def test_chunk_pdf_largo_se_divide_sin_cambiar_indices() -> None:
    contrato = contrato_base(tdr_texto=("a" * 2000) + "\n" + ("b" * 2000))
    chunks = chunks_de_pdf(contrato, chunk_index_offset=7)
    assert [chunk["chunk_index"] for chunk in chunks] == [7, 8]
    assert [chunk["tipo"] for chunk in chunks] == ["TDR PDF (1/2)", "TDR PDF (2/2)"]


def test_chunks_api_preservan_descripcion_items_y_metadata() -> None:
    items = [{
        "cod_cubso": "123",
        "nom_cubso": "SERVICIO",
        "cantidad": 2,
        "unidad": "unidad",
        "distrito": "Lima",
        "descripcion": "Implementación",
    }]
    contrato = contrato_base(items_json=json.dumps(items))
    chunks = chunks_de_contrato(contrato)
    assert [chunk["chunk_index"] for chunk in chunks] == [0, 1, 2]
    assert [chunk["tipo"] for chunk in chunks] == [
        "Descripción general",
        "Ítem técnico 1",
        "Metadata",
    ]
    assert all(chunk["contrato_id"] == 42 for chunk in chunks)
    assert all(chunk["fuente"] == "api" for chunk in chunks)
    assert "CUBSO: 123 - SERVICIO" in chunks[1]["texto"]
    assert "Área usuaria: Tecnología" in chunks[2]["texto"]


def test_items_invalidos_no_impiden_chunk_metadata() -> None:
    chunks = chunks_de_contrato(contrato_base(descripcion="", items_json="{inválido"))
    assert len(chunks) == 1
    assert chunks[0]["tipo"] == "Metadata"
    assert chunks[0]["chunk_index"] == 0


def test_embed_pdf_no_incluye_membrete_de_display() -> None:
    contrato = contrato_base()
    display = f"{encabezado_pdf(contrato)}\nPERÚ\nOBJETO:\nServicio real"
    assert embed_text_pdf(contrato, display).endswith("OBJETO:\nServicio real")
    assert "PERÚ" not in embed_text_pdf(contrato, display)
