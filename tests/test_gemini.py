from __future__ import annotations

from seace_monitor.gemini import (
    EMBED_USD_PER_M,
    FLASH_USD_IN_PER_M,
    FLASH_USD_OUT_PER_M,
    extract_gemini_text,
    fecha_lima,
    l2_normalize,
    usd_embed,
    usd_flash,
)


def test_fecha_lima_es_iso():
    import re
    out = fecha_lima()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", out)


def test_extract_gemini_text_salta_thought():
    body = {
        "candidates": [{
            "content": {"parts": [
                {"text": "hola", "thought": False},
                {"text": "razonando", "thought": True},
                {"text": " mundo"},
            ]},
        }],
    }
    assert extract_gemini_text(body) == "hola mundo"


def test_extract_gemini_text_vacio():
    assert extract_gemini_text({}) == ""


def test_l2_normalize_unitario():
    v = l2_normalize([3.0, 4.0])
    assert abs(v[0] - 0.6) < 1e-9
    assert abs(v[1] - 0.8) < 1e-9


def test_l2_normalize_vector_cero_no_divide():
    assert l2_normalize([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]


def test_usd_flash():
    # 1M in + 1M out = 0.50 + 3.00
    assert abs(usd_flash(1_000_000, 1_000_000) - 3.50) < 1e-9


def test_usd_embed():
    assert abs(usd_embed(1_000_000) - 0.0375) < 1e-9


def test_constantes_precios():
    assert FLASH_USD_IN_PER_M == 0.50
    assert FLASH_USD_OUT_PER_M == 3.00
    assert EMBED_USD_PER_M == 0.0375
