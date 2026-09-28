"""Regresiones AUD-004: corpus, secretos y trazabilidad con datos sintéticos."""
import math
from types import SimpleNamespace

import httpx
import pytest

from test_ia_resolver import make_resolver, fila_endpoint
from test_ia_openai_providers import FakeClient, response
from test_ia_pipeline import cfg_openai
from seace_monitor.ia import pipeline
from seace_monitor.ia.resolver import ResolverCfg
from seace_monitor.ia.openai import post_openai
from seace_monitor.ia.errores import ErrorProveedor
from seace_monitor.embeddings.openai_provider import solicitar_embeddings_openai
from seace_monitor.ocr.gemini_provider import OCR_ACTIVO, GEMINI_FLASH


@pytest.mark.parametrize('corpus', [None, '', 'qwen-tev4-1536'])
def test_embeddings_no_fallback_sin_corpus_legacy_comprobado(corpus):
    resolver = make_resolver([], meta_corpus=corpus)
    with pytest.raises(RuntimeError):
        resolver.resolver('embeddings')


def test_embeddings_no_fallback_si_modelo_activo_incompatible():
    resolver = make_resolver([fila_endpoint('embeddings', dimensiones=1536,
        espacio='qwen-tev4-1536')])
    with pytest.raises(RuntimeError):
        resolver.resolver('embeddings')


def test_fallo_lectura_no_filtra_texto_y_bloquea_embeddings(capsys):
    def fail(*a, **kw):
        raise httpx.ConnectError('secret-synthetic-url-and-document')
    resolver = ResolverCfg(url='https://db.test', key='synthetic', get=fail)
    assert resolver.resolver('ocr') is None
    assert 'secret-synthetic' not in capsys.readouterr().out
    with pytest.raises(RuntimeError):
        resolver.resolver('embeddings')


@pytest.mark.parametrize('error', [
    response(401, {'error': {'message': 'secret-synthetic-token'}}),
    httpx.ConnectError('secret-synthetic-token'),
])
def test_transporte_no_expone_respuestas_o_excepciones(error):
    with pytest.raises(ErrorProveedor) as caught:
        post_openai(FakeClient([error]), 'https://p.test', 'synthetic-key', {},
                    timeout=1, proveedor='qwen')
    assert 'secret-synthetic' not in str(caught.value)


@pytest.mark.parametrize('items', [
    [{'index': 0, 'embedding': [1., 0.]}, {'index': 0, 'embedding': [0., 1.]}],
    [{'index': 1, 'embedding': [1., 0.]}, {'index': 2, 'embedding': [0., 1.]}],
    [{'embedding': [1., 0.]}, {'index': 1, 'embedding': [0., 1.]}],
    [{'index': 0, 'embedding': [0., 0.]}, {'index': 1, 'embedding': [0., 1.]}],
    [{'index': 0, 'embedding': ['nan', 0.]}, {'index': 1, 'embedding': [0., 1.]}],
    [{'index': 0, 'embedding': [True, 0.]}, {'index': 1, 'embedding': [0., 1.]}],
])
def test_embeddings_rechaza_indices_y_vectores_invalidos(items):
    with pytest.raises(ErrorProveedor):
        solicitar_embeddings_openai(FakeClient([response(200, {'data': items})]),
            ['a', 'b'], 'synthetic', url='https://p.test', modelo='m',
            dimensiones=2, backoff=())


def test_ocr_vuelta_env_restablece_identidad_y_precio():
    previous = OCR_ACTIVO.copy()
    try:
        pipeline.fijar_ocr_activo(cfg_openai())
        pipeline.fijar_ocr_activo(None)
        assert OCR_ACTIVO['modelo'] == GEMINI_FLASH
        assert OCR_ACTIVO['version_config'] == 0
    finally:
        OCR_ACTIVO.update(previous)


def test_precio_cero_es_precio_valido():
    previous = OCR_ACTIVO.copy()
    try:
        pipeline.fijar_ocr_activo(cfg_openai(precio={'in': 0, 'out': 0}))
        assert OCR_ACTIVO['usd_in'] == OCR_ACTIVO['usd_out'] == 0
        extras = pipeline.extras_embeddings(cfg_openai(endpoint='embeddings',
            dimensiones=1536, precio={'in': 0}))
        assert extras['precio_in'] == 0
    finally:
        OCR_ACTIVO.update(previous)


def test_timeout_embeddings_dinamico_llega_al_transporte():
    cfg = cfg_openai(endpoint='embeddings', dimensiones=1536, timeout_ms=2300)
    extras = pipeline.extras_embeddings(cfg)
    client = FakeClient([response(200, {'data': [{'index': 0,
        'embedding': [1.] + [0.] * 1535}]})])
    extras['solicitar'](client, ['a'], 'synthetic')
    assert client.calls[0][1]['timeout'] == 2.3


def test_clasificacion_uso_ia_precio_modelo_version(tmp_path, monkeypatch):
    import clasificar_gemini as cli
    from seace_monitor.classification import pasada
    rows = []
    class DB:
        def table(self, name):
            assert name == 'uso_ia'
            return self
        def insert(self, row):
            rows.append(row)
            return self
        def execute(self): return None
    cfg = cfg_openai(endpoint='clasificar', modelo='qwen-test',
                     precio={'in': 2, 'out': 3}, version_config=19)
    monkeypatch.setattr(cli, 'resolver_cfg', lambda ep: cfg)
    monkeypatch.setattr(pasada, 'assert_cuota_c4', lambda *a, **kw: None)
    monkeypatch.setattr(pasada, 'registrar_llamada_c4', lambda *a, **kw: None)
    composed = cli.construir_config(SimpleNamespace(max_llamadas_dia=2), DB())
    client = FakeClient([response(200, {'choices': [{'message': {'content': '[]'}}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})])
    composed.clasificar_lote(client, [], armar_prompt=lambda x: 'test')
    assert len(rows) == 1
    assert rows[0]['modelo'] == 'qwen-test'
    assert rows[0]['detalle']['version_config'] == 19
    assert rows[0]['costo_usd'] == pytest.approx(.00026)
    assert 'db-key' not in repr(composed)


def test_clasificacion_reintenta_salida_no_parseable_sin_filtrar(capsys):
    import json
    from seace_monitor.classification.openai_provider import clasificar_lote_openai
    ok = {'choices': [{'message': {'content': '[]'}}]}
    bad = {'choices': [{'message': {'content': '{"secret-synthetic'}}]}
    client = FakeClient([response(200, bad), response(200, ok)])
    assert clasificar_lote_openai(client, [], system_prompt='s', schema={},
        armar_prompt=lambda x: 'p', api_key='k', url='u',
        parse_response=json.loads, backoff=(0.0,), sleep=lambda s: None) == []
    assert len(client.calls) == 2
    assert 'secret-synthetic' not in capsys.readouterr().out


def test_clasificacion_agota_reintentos_con_mensaje_saneado():
    import json
    from seace_monitor.classification.openai_provider import clasificar_lote_openai
    bad = {'choices': [{'message': {'content': '{"secret-synthetic'}}]}
    with pytest.raises(RuntimeError, match='clasificar_lote fallo') as caught:
        clasificar_lote_openai(FakeClient([response(200, bad)] * 2), [],
            system_prompt='s', schema={}, armar_prompt=lambda x: 'p',
            api_key='k', url='u', parse_response=json.loads,
            backoff=(0.0,), sleep=lambda s: None)
    assert 'secret-synthetic' not in str(caught.value)


def test_habilitacion_documental_no_aborta_si_corpus_no_verificable(monkeypatch):
    import descargar_requerimiento as dr
    import extraer_contenedores as ec
    from seace_monitor.ia.resolver import ConfigEmbeddingInsegura

    def inseguro(endpoint):
        if endpoint == 'embeddings':
            raise ConfigEmbeddingInsegura('corpus no verificable')
        return None
    assert pipeline.embeddings_configurados(inseguro) is False
    monkeypatch.setattr(ec, 'GEMINI_API_KEY', '')
    monkeypatch.setattr(ec, 'cfg_resuelta', inseguro)
    assert ec.ia_habilitada() is False
    monkeypatch.setattr(dr, 'resolver_cfg', inseguro)
    assert pipeline.embeddings_configurados(dr.resolver_cfg) is False
