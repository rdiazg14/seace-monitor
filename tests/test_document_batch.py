"""Pruebas del caso de uso de descarga por lotes, sin red ni Supabase."""

from __future__ import annotations

from types import SimpleNamespace

from seace_monitor.documents import batch
from seace_monitor.documents.pdf_extraction import NecesitaOcr, PdfExtractError
from seace_monitor.documents.seace_files import NoEsPdf, PdfTruncado, SinPdf


COUNTS = {
    "vigentes": 6,
    "pendientes": 6,
    "pendiente_ocr": 0,
    "ya_descargados": 0,
}


def _kwargs(tmp_path, **overrides):
    values = {
        "ids": [],
        "limit": 10,
        "modo": "todos",
        "permitir_ocr": True,
        "dry_run": False,
        "headed": False,
        "compacto": True,
        "rpm": 0.0,
        "delay_s": 0.35,
        "temp_prefix": "seace-test-",
        "listar_url": "https://listar/{id}",
        "descargar_url": "https://descargar/{id}",
        "gemini_habilitado": False,
        "procesar": lambda *_a, **_kw: {},
        "imprimir_linea": lambda *_a: None,
        "imprimir_resultado": lambda *_a: None,
        "imprimir_sin_pdf": lambda *_a: None,
        "temp_dir": lambda: str(tmp_path),
        "sleep": lambda _seconds: None,
    }
    values.update(overrides)
    return values


def test_cola_vacia_persiste_resumen_y_no_abre_http(tmp_path, monkeypatch):
    summaries = []
    monkeypatch.setattr(batch, "conteo_pdf", lambda _supa: COUNTS)
    monkeypatch.setattr(batch, "pendientes_pdf", lambda *_a, **_kw: [])
    monkeypatch.setattr(batch, "reporte_jsonl", lambda: {"lineas": 0})
    monkeypatch.setattr(batch, "columnas_extraccion_ok", lambda _supa: False)
    monkeypatch.setattr(batch, "escribir_resumen", lambda _supa, stats: summaries.append(stats))

    stats = batch.ejecutar_descarga_batch(
        object(),
        **_kwargs(
            tmp_path,
            http_factory=lambda **_kw: (_ for _ in ()).throw(AssertionError("HTTP abierto")),
        ),
    )

    assert stats == summaries[0]
    assert stats["ok"] == 0
    assert stats["lineas"] == 0
    assert stats["modo"] == "todos"


def test_batch_clasifica_resultados_y_persiste_cada_rama(tmp_path, monkeypatch):
    contracts = [
        {"id": 1, "descripcion_contrato": "ok"},
        {"id": 2, "descripcion_contrato": "ocr"},
        {"id": 3, "descripcion_contrato": "sin pdf"},
        {"id": 4, "descripcion_contrato": "no pdf"},
        {"id": 5, "descripcion_contrato": "extract"},
        {"id": 6, "descripcion_contrato": "unexpected", "req_url": "sin_pdf"},
        {"id": 7, "descripcion_contrato": "truncado"},
    ]
    calls: dict[str, list] = {
        "ok": [], "ocr": [], "sin_pdf": [], "storage": [], "rechazo": [],
        "linea": [], "summary": [], "sleep": [], "truncado": [],
    }
    closed = []

    monkeypatch.setattr(batch, "pendientes_pdf", lambda *_a, **_kw: contracts)
    monkeypatch.setattr(batch, "conteo_pdf", lambda _supa: COUNTS)
    monkeypatch.setattr(batch, "columnas_extraccion_ok", lambda _supa: True)
    monkeypatch.setattr(batch, "reporte_extraccion", lambda _supa: {
        "nativo_puro": 1, "mixto": 0, "imagen_total": 0,
        "paginas_ocr_reales": 0, "paginas_nativas": 1,
        "paginas_totales": 1, "sin_pdf": 1,
        "pendiente_ocr_viejo": 1, "sin_tipo": 3,
    })
    monkeypatch.setattr(batch, "guardar_ok", lambda _supa, row: calls["ok"].append(row))
    monkeypatch.setattr(
        batch,
        "guardar_pendiente_ocr",
        lambda _supa, cid, meta: calls["ocr"].append((cid, meta)),
    )
    monkeypatch.setattr(batch, "guardar_sin_pdf", lambda _supa, cid: calls["sin_pdf"].append(cid))
    monkeypatch.setattr(
        batch,
        "guardar_pdf_truncado",
        lambda _supa, cid, c: calls["truncado"].append((cid, c)),
    )
    monkeypatch.setattr(
        batch,
        "persistir_storage_si_hay",
        lambda _supa, cid, meta: calls["storage"].append((cid, meta)),
    )
    monkeypatch.setattr(
        batch,
        "registrar_rechazo",
        lambda _supa, payload, motivo, **kw: calls["rechazo"].append((payload, motivo, kw)),
    )
    monkeypatch.setattr(
        batch,
        "escribir_resumen",
        lambda _supa, stats: calls["summary"].append(stats),
    )

    def process(_http, contract, **_kwargs):
        cid = contract["id"]
        if cid == 1:
            return {
                "id": 1, "tdr_tipo_extraccion": "nativo_puro",
                "n_paginas": 1, "n_paginas_nativas": 1,
                "n_paginas_ocr": 0, "ocr_paginas": [], "chars_final": 10,
            }
        if cid == 2:
            raise NecesitaOcr({"n_paginas": 2, "ocr_paginas": [1, 2]})
        if cid == 3:
            raise SinPdf([{"idContratoArchivo": 30}])
        if cid == 4:
            raise NoEsPdf("html")
        if cid == 5:
            raise PdfExtractError("fallo", {"pdf_storage_path": "tdr/5.pdf"})
        if cid == 7:
            raise PdfTruncado("n=704349 sin startxref/%%EOF en la cola")
        raise RuntimeError("inesperado")

    moments = iter((10.0, 12.9))
    stats = batch.ejecutar_descarga_batch(
        object(),
        **_kwargs(
            tmp_path,
            procesar=process,
            imprimir_linea=lambda *args: calls["linea"].append(args),
            http_factory=lambda **_kw: SimpleNamespace(close=lambda: closed.append(True)),
            now=lambda: next(moments),
            sleep=calls["sleep"].append,
        ),
    )

    assert len(calls["ok"]) == 1
    assert calls["ocr"][0][0] == 2
    assert calls["sin_pdf"] == [3, 4]
    assert calls["truncado"] == [(7, contracts[6])]
    assert calls["storage"] == [(5, {"pdf_storage_path": "tdr/5.pdf"})]
    assert len(calls["rechazo"]) == 5
    motivos = [motivo for _payload, motivo, _kw in calls["rechazo"]]
    assert "pdf truncado en origen" in motivos
    assert len(calls["sleep"]) == len(contracts)
    assert closed == [True]
    assert stats is calls["summary"][0]
    assert stats["ok"] == 1
    assert stats["skip_ocr"] == 1
    assert stats["ocr_paginas_estimadas"] == 2
    assert stats["sin_pdf_cola"] == 1
    assert stats["pdf_truncado"] == 1
    assert stats["err"] == 2
    assert stats["elapsed_s"] == 2


def test_dry_run_no_persiste_resultados(tmp_path, monkeypatch):
    contract = {"id": 7, "descripcion_contrato": "dry"}
    monkeypatch.setattr(batch, "pendientes_pdf", lambda *_a, **_kw: [contract])
    monkeypatch.setattr(batch, "conteo_pdf", lambda _supa: COUNTS)
    monkeypatch.setattr(batch, "columnas_extraccion_ok", lambda _supa: False)
    monkeypatch.setattr(batch, "guardar_ok", lambda *_a: (_ for _ in ()).throw(AssertionError("write")))
    monkeypatch.setattr(batch, "escribir_resumen", lambda *_a: None)
    moments = iter((1.0, 1.0))

    stats = batch.ejecutar_descarga_batch(
        object(),
        **_kwargs(
            tmp_path,
            dry_run=True,
            procesar=lambda _http, _contract, **kwargs: {
                "id": 7, "tdr_tipo_extraccion": "mixto", "n_paginas": 2,
                "n_paginas_nativas": 1, "n_paginas_ocr": 1,
                "ocr_paginas": [2], "chars_final": 20,
                "supa_recibido": kwargs["supa"],
            },
            http_factory=lambda **_kw: SimpleNamespace(close=lambda: None),
            now=lambda: next(moments),
        ),
    )

    assert stats["ok"] == 1
    assert stats["mixto_cola"] == 1
    assert stats["dry_run"] is True
