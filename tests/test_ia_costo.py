"""FIX-008: costo por precio de config validado y tramo por tokens de entrada.

Misma regla y casos límite que ``seace-ai-proxy/src/telemetry/costo.test.ts``.
Sin red ni Supabase: dobles en memoria.
"""
from __future__ import annotations

import pytest

from seace_monitor.ia import pipeline
from seace_monitor.ia.contracts import ModeloCfg
from seace_monitor.ia.costo import estimar, parse_precio, tarifa
from seace_monitor.ocr import cuota as ocr_cuota
from seace_monitor.ocr.gemini_provider import OCR_ACTIVO

QWEN_SEED = {
    "moneda": "USD", "in": 0.03, "out": 0.13,
    "tiers": [
        {"hasta_input_tokens": 32000, "in": 0.03, "out": 0.13},
        {"hasta_input_tokens": 256000, "in": 0.1, "out": 0.4},
        {"hasta_input_tokens": 1000000, "in": 0.2, "out": 0.8},
    ],
    "fuente": "alibabacloud.com/help/en/model-studio/model-pricing (international)",
    "fecha": "2026-09-27",
}


@pytest.fixture(autouse=True)
def ocr_env():
    previo = dict(OCR_ACTIVO)
    yield
    OCR_ACTIVO.clear()
    OCR_ACTIVO.update(previo)


def cfg(**kw) -> ModeloCfg:
    base = dict(endpoint="ocr", proveedor="novita", tipo_api="openai",
                modelo="deepseek/deepseek-ocr-2", base_url="https://n.test",
                api_key="k", precio={"moneda": "USD", "in": 0.03, "out": 0.03},
                version_config=9)
    base.update(kw)
    return ModeloCfg(**base)


@pytest.mark.parametrize("raw", [
    None, {}, [], "x", {"in": -1, "out": 0}, {"in": float("nan")}, {"in": "0.03"},
    {"moneda": "PEN", "in": 0.1, "out": 0.1}, {"in": 0.1, "out": float("inf")},
    {"in": True, "out": 0},
    {"in": 0.03, "tiers": [{"hasta_input_tokens": 256000, "in": 0.1},
                           {"hasta_input_tokens": 32000, "in": 0.03}]},
    {"in": 0.03, "tiers": [{"hasta_input_tokens": 32000, "in": 0.03},
                           {"hasta_input_tokens": 32000, "in": 0.1}]},
    {"in": 0.03, "tiers": [{"hasta_input_tokens": 0, "in": 0.03}]},
    {"in": 0.03, "tiers": [{"hasta_input_tokens": 32000.5, "in": 0.03}]},
    {"in": 0.03, "tiers": [{"hasta_input_tokens": 32000, "in": -1}]},
    {"in": 0.03, "tiers": "no-lista"},
])
def test_parse_precio_rechaza_invalidos(raw):
    assert parse_precio(raw) is None


def test_parse_precio_acepta_seed_plano_y_cero():
    p = parse_precio(QWEN_SEED)
    assert len(p["tramos"]) == 3 and p["fecha"] == "2026-09-27"
    assert parse_precio({"in": 0.07}) == {"in": 0.07, "out": 0.0, "tramos": []}
    assert parse_precio({"in": 0, "out": 0}) == {"in": 0.0, "out": 0.0, "tramos": []}


@pytest.mark.parametrize("tokens,tramo,excede", [
    (0, 0, False), (32000, 0, False), (32001, 1, False), (256000, 1, False),
    (256001, 2, False), (1_000_000, 2, False), (1_000_001, 2, True),
])
def test_tarifa_limites_exactos(tokens, tramo, excede):
    assert tarifa(parse_precio(QWEN_SEED), tokens)[2:] == (tramo, excede)


def test_estimar_tramo_y_trazabilidad():
    usd, det = estimar(parse_precio(QWEN_SEED), 100_000, 3_000)
    assert usd == pytest.approx(100_000 / 1e6 * 0.1 + 3_000 / 1e6 * 0.4)
    assert det == {"precio_fuente": "config", "costo_estimado": True,
                   "precio_tramo": 1, "precio_fecha": "2026-09-27"}
    assert estimar(None, 10, 10) == (None, {"precio_fuente": "desconocido", "costo_estimado": True})


def test_estimar_acumulado_elige_tramo_por_promedio_de_llamada():
    # 10 páginas de 20K = 200K acumulados, pero cada llamada cae en el tramo 0.
    usd, det = estimar(parse_precio(QWEN_SEED), 200_000, 0, llamadas=10)
    assert det["precio_tramo"] == 0
    assert usd == pytest.approx(200_000 / 1e6 * 0.03)


def test_ocr_config_invalida_no_inventa_precio_gemini():
    pipeline.fijar_ocr_activo(cfg(precio={"moneda": "PEN", "in": 1}))
    assert OCR_ACTIVO["usd_in"] is None and OCR_ACTIVO["precio"] is None
    usd, det = ocr_cuota.costo_ocr(1000, 100)
    assert usd is None and det["precio_fuente"] == "desconocido"


def test_ocr_env_usa_tabla_y_config_su_precio():
    pipeline.fijar_ocr_activo(None)
    usd, det = ocr_cuota.costo_ocr(1_000_000, 0)
    assert usd == pytest.approx(0.75) and det["precio_fuente"] == "tabla"
    pipeline.fijar_ocr_activo(cfg())
    usd, det = ocr_cuota.costo_ocr(1_000_000, 1_000_000)
    assert usd == pytest.approx(0.06) and det["precio_fuente"] == "config"


def test_registrar_ocr_sin_precio_traza_null_y_no_suma_gasto(tmp_path):
    rows = []

    class DB:
        def table(self, name):
            self.name = name
            return self

        def insert(self, row):
            rows.append(row)
            return self

        def upsert(self, *a, **kw):
            return self

        def execute(self):
            return None

    pipeline.fijar_ocr_activo(cfg(precio={}))
    cuota = {"requests": 0, "usd_est": 1.5}
    ocr_cuota.registrar_ocr_ok(DB(), cuota, 99, last_usage={"prompt": 500, "candidates": 50},
                               path=tmp_path / "cuota.json")
    assert cuota["usd_est"] == 1.5
    uso = [r for r in rows if r.get("componente") == "ocr"][0]
    assert uso["costo_usd"] is None
    assert uso["detalle"]["precio_fuente"] == "desconocido"


def test_clasificacion_qwen_aplica_tramo_superior(monkeypatch):
    from seace_monitor.classification import pasada

    rows = []

    class DB:
        def table(self, name):
            return self

        def insert(self, row):
            rows.append(row)
            return self

        def execute(self):
            return None

    monkeypatch.setattr(pasada, "registrar_llamada_c4", lambda *a, **kw: None)
    monkeypatch.setattr(pasada, "acumular_tokens", lambda *a, **kw: None)

    def transporte(client, lote, *, on_success, **kw):
        on_success({"usageMetadata": {"promptTokenCount": 40_000, "candidatesTokenCount": 1_000}})
        return []

    pasada.clasificar_lote(None, [], supa=DB(), stats={}, api_key="k", url="u",
                           transporte=transporte, modelo="qwen3.7-flash",
                           precio=QWEN_SEED, version_config=21)
    assert rows[0]["costo_usd"] == pytest.approx(40_000 / 1e6 * 0.1 + 1_000 / 1e6 * 0.4)
    assert rows[0]["detalle"]["precio_tramo"] == 1
    assert rows[0]["detalle"]["version_config"] == 21
