"""Caracterización del entrypoint histórico ``descargar_requerimiento``.

Cubre el comportamiento observado con dobles: sidecar JSONL (última línea por id
gana), cuota OCR (BD → fallback local → reinicio diario), sync de metadatos
(probe, abortos y huérfanos), reportes, persistencia documental y las salidas
tempranas de ``run_ocr_selectivo``. Nada toca red ni servicios reales.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import descargar_requerimiento as dr


# ── Dobles ────────────────────────────────────────────────────────────────────

class Q:
    """Query encadenable que graba operaciones y delega en un responder."""

    def __init__(self, supa, table: str):
        self.supa = supa
        self.table_name = table
        self.ops: list[tuple] = []
        self.update_payload = None
        self.insert_payload = None
        self.upsert_payload = None
        self.upsert_kw: dict = {}

    def _op(self, name: str, *args, **kwargs) -> "Q":
        self.ops.append((name, args, kwargs))
        return self

    def select(self, *a, **kw):
        return self._op("select", *a, **kw)

    def eq(self, *a):
        return self._op("eq", *a)

    def in_(self, *a):
        return self._op("in_", *a)

    def is_(self, *a):
        return self._op("is_", *a)

    def or_(self, *a):
        return self._op("or_", *a)

    def order(self, *a, **kw):
        return self._op("order", *a, **kw)

    def range(self, *a):
        return self._op("range", *a)

    def limit(self, *a):
        return self._op("limit", *a)

    def maybe_single(self):
        return self._op("maybe_single")

    def update(self, payload):
        self.update_payload = payload
        return self

    def insert(self, payload):
        self.insert_payload = payload
        return self

    def upsert(self, payload, **kw):
        self.upsert_payload = payload
        self.upsert_kw = kw
        return self

    def has_op(self, name: str, *args) -> bool:
        return any(
            op[0] == name and op[1][:len(args)] == args for op in self.ops
        )

    def execute(self):
        return self.supa._exec(self)


class FakeSupa:
    def __init__(self, responder=None):
        self.queries: list[Q] = []
        self.responder = responder or (lambda q: SimpleNamespace(data=[], count=0))

    def table(self, name: str) -> Q:
        q = Q(self, name)
        self.queries.append(q)
        return q

    def _exec(self, q: Q):
        return self.responder(q)

    def updates(self) -> list[Q]:
        return [q for q in self.queries if q.update_payload is not None]

    def inserts(self, table: str) -> list[Q]:
        return [q for q in self.queries
                if q.table_name == table and q.insert_payload is not None]


@pytest.fixture
def meta_log(tmp_path, monkeypatch):
    path = tmp_path / "tdr_extraccion.jsonl"
    monkeypatch.setattr(dr, "META_LOG", path)
    return path


@pytest.fixture
def cuota_path(tmp_path, monkeypatch):
    path = tmp_path / "flash_ocr_cuota.json"
    monkeypatch.setattr(dr, "CUOTA_OCR_PATH", path)
    return path


@pytest.fixture
def hoy_fijo(monkeypatch):
    monkeypatch.setattr(dr, "fecha_lima", lambda: "2026-09-26")
    return "2026-09-26"


# ── Sidecar JSONL ─────────────────────────────────────────────────────────────

def test_meta_local_ultima_linea_por_id_gana(meta_log):
    dr.registrar_meta_local({"id": 1, "n_paginas": 3, "ocr_paginas": [2]})
    dr.registrar_meta_local({
        "id": 1,
        "tdr_tipo_extraccion": "imagen_total",
        "ocr_paginas": [1, 2, 3],
        "n_paginas": 3,
    })
    dr.registrar_meta_local({"id": 2, "n_paginas": 5, "ocr_paginas": []})

    by_id = dr.meta_local_por_id()

    assert by_id[1]["tdr_tipo_extraccion"] == "imagen_total"
    assert by_id[1]["paginas_ocr_pendientes"] == [1, 2, 3]
    assert by_id[2]["tdr_tipo_extraccion"] == "nativo_puro"
    assert by_id[2]["pdf_es_imagen"] is False


def test_registrar_meta_local_deriva_campos(meta_log):
    dr.registrar_meta_local({
        "id": 7, "n_paginas": 4, "ocr_paginas": [2, 4],
        "ocr_hechas": [2], "n_paginas_nativas": 2, "n_paginas_ocr": 2,
        "chars_final": 1234,
    })

    rec = json.loads(meta_log.read_text(encoding="utf-8").splitlines()[0])

    assert rec == {
        "id": 7,
        "tdr_tipo_extraccion": "mixto",
        "paginas_ocr_pendientes": [2, 4],
        "paginas_ocr_hechas": [2],
        "tdr_n_paginas": 4,
        "tdr_n_paginas_nativas": 2,
        "tdr_n_paginas_ocr": 2,
        "pdf_es_imagen": True,
        "chars_final": 1234,
    }


def test_reporte_jsonl_vacio_y_agregado(meta_log):
    assert dr.reporte_jsonl() == {
        "nativo_puro": 0, "mixto": 0, "imagen_total": 0,
        "paginas_ocr_reales": 0, "paginas_nativas": 0,
        "paginas_totales": 0, "contratos": 0,
    }

    meta_log.write_text(
        "\n".join([
            json.dumps({"id": 1, "tdr_tipo_extraccion": "nativo_puro",
                        "tdr_n_paginas": 2, "tdr_n_paginas_nativas": 2,
                        "tdr_n_paginas_ocr": 0}),
            json.dumps({"id": 2, "tdr_tipo_extraccion": "mixto",
                        "tdr_n_paginas": 4, "tdr_n_paginas_nativas": 2,
                        "tdr_n_paginas_ocr": 2}),
            json.dumps({"id": 2, "tdr_tipo_extraccion": "imagen_total",
                        "tdr_n_paginas": 4, "tdr_n_paginas_nativas": 0,
                        "tdr_n_paginas_ocr": 4}),
            "",
        ]) + "\n",
        encoding="utf-8",
    )

    assert dr.reporte_jsonl() == {
        "nativo_puro": 1, "mixto": 0, "imagen_total": 1,
        "paginas_ocr_reales": 4, "paginas_nativas": 2,
        "paginas_totales": 6, "contratos": 2,
    }


def test_payload_extraccion_respeta_aliases_y_derivaciones():
    rec = {
        "tdr_tipo_extraccion": "mixto",
        "ocr_paginas": [3],
        "ocr_hechas": [1],
        "n_paginas": 4,
        "n_paginas_nativas": 3,
        "n_paginas_ocr": 1,
    }

    payload = dr.payload_extraccion(rec)

    assert payload == {
        "pdf_es_imagen": True,
        "tdr_tipo_extraccion": "mixto",
        "paginas_ocr_pendientes": [3],
        "paginas_ocr_hechas": [1],
        "tdr_n_paginas": 4,
        "tdr_n_paginas_nativas": 3,
        "tdr_n_paginas_ocr": 1,
    }
    # Sin tipo y sin pdf_es_imagen → pdf_es_imagen=None (no se inventa)
    assert dr.payload_extraccion({})["pdf_es_imagen"] is None


# ── Cuota OCR ─────────────────────────────────────────────────────────────────

def test_cuota_remota_gana_sobre_local(cuota_path, hoy_fijo):
    cuota_path.write_text(json.dumps({
        "fecha": hoy_fijo, "requests": 99,
    }), encoding="utf-8")
    supa = FakeSupa(lambda q: SimpleNamespace(data={
        "requests": 5, "prompt_tokens": 10, "out_tokens": 4, "usd_est": 0.01,
    }))

    cuota = dr.cargar_cuota_ocr(supa)

    assert cuota == {
        "fecha": hoy_fijo, "requests": 5, "prompt_tokens": 10,
        "out_tokens": 4, "usd_est": 0.01,
    }
    assert supa.queries[0].table_name == "pipeline_cuota_ocr"
    assert supa.queries[0].has_op("eq", "fecha_lima", hoy_fijo)
    assert supa.queries[0].has_op("maybe_single")


def test_cuota_falla_remota_cae_al_local(cuota_path, hoy_fijo):
    cuota_path.write_text(json.dumps({
        "fecha": hoy_fijo, "requests": 3, "usd_est": 0.02,
    }), encoding="utf-8")

    def falla(q):
        raise RuntimeError("PostgREST down")

    cuota = dr.cargar_cuota_ocr(FakeSupa(falla))

    assert cuota["requests"] == 3
    assert cuota["prompt_tokens"] == 0  # setdefault rellena contadores
    assert cuota["usd_est"] == 0.02


def test_cuota_reinicia_con_fecha_distinta(cuota_path, hoy_fijo):
    cuota_path.write_text(json.dumps({
        "fecha": "2026-09-25", "requests": 4000,
    }), encoding="utf-8")

    cuota = dr.cargar_cuota_ocr(FakeSupa())

    assert cuota == {
        "fecha": hoy_fijo, "requests": 0, "prompt_tokens": 0,
        "out_tokens": 0, "usd_est": 0.0,
    }


def test_cuota_sin_fuentes_arranca_en_cero(cuota_path, hoy_fijo):
    assert dr.cargar_cuota_ocr(FakeSupa())["requests"] == 0
    assert dr.cargar_cuota_ocr(None)["fecha"] == hoy_fijo


def test_guardar_cuota_upsert_remoto_y_siempre_local(cuota_path):
    supa = FakeSupa()

    dr.guardar_cuota_ocr(supa, {
        "fecha": "2026-09-26", "requests": 2, "prompt_tokens": 5,
        "out_tokens": 3, "usd_est": 0.001,
    })

    q = supa.queries[0]
    assert q.table_name == "pipeline_cuota_ocr"
    assert q.upsert_payload["fecha_lima"] == "2026-09-26"
    assert q.upsert_payload["requests"] == 2
    assert "updated_at" in q.upsert_payload
    assert q.upsert_kw == {"on_conflict": "fecha_lima"}
    assert json.loads(cuota_path.read_text(encoding="utf-8"))["requests"] == 2


def test_guardar_cuota_falla_remota_igual_escribe_local(cuota_path):
    def falla(q):
        raise RuntimeError("sin tabla")

    dr.guardar_cuota_ocr(FakeSupa(falla), {"fecha": "x", "requests": 1})

    assert json.loads(cuota_path.read_text(encoding="utf-8"))["requests"] == 1


def test_registrar_ocr_ok_acumula_tokens_uso_ia_y_tope(cuota_path):
    dr.LAST_OCR_USAGE.clear()
    dr.LAST_OCR_USAGE.update({"prompt": 100, "candidates": 20})
    supa = FakeSupa()
    cuota = {"fecha": "2026-09-26", "requests": 0,
             "prompt_tokens": 0, "out_tokens": 0, "usd_est": 0.0}

    with pytest.raises(dr.CupoFlash):
        dr.registrar_ocr_ok(supa, cuota, max_dia=1)

    assert cuota["requests"] == 1
    assert cuota["prompt_tokens"] == 100
    assert cuota["out_tokens"] == 20
    assert cuota["usd_est"] > 0
    uso = supa.inserts("uso_ia")
    assert len(uso) == 1
    assert uso[0].insert_payload["componente"] == "ocr"
    assert uso[0].insert_payload["tokens_total"] == 120
    dr.LAST_OCR_USAGE.clear()


def test_registrar_ocr_ok_bajo_tope_no_lanza(cuota_path):
    dr.LAST_OCR_USAGE.clear()
    dr.LAST_OCR_USAGE.update({"prompt": 1, "candidates": 1})
    cuota = {"fecha": "2026-09-26", "requests": 0}

    dr.registrar_ocr_ok(None, cuota, max_dia=6000)

    assert cuota["requests"] == 1
    dr.LAST_OCR_USAGE.clear()


# ── Anexar / helpers puros ─────────────────────────────────────────────────────

def test_anexar_ocr_a_tdr_idempotente_y_marca():
    assert dr.anexar_ocr_a_tdr("", 2, "texto") == "--- pagina 2 (ocr) ---\ntexto"
    base = "nativo"
    out = dr.anexar_ocr_a_tdr(base, 3, "  ocr  ")
    assert out == "nativo\n\n--- pagina 3 (ocr) ---\nocr"
    # misma marca ya presente → no duplica
    assert dr.anexar_ocr_a_tdr(out, 3, "otro") == out


def test_as_int_list_tolerante():
    assert dr._as_int_list(None) == []
    assert dr._as_int_list("[1, 2]") == [1, 2]
    assert dr._as_int_list(["3", "x", 4]) == [3, 4]


def test_parse_ids():
    assert dr.parse_ids("") == []
    assert dr.parse_ids("1, 2\n3") == [1, 2, 3]


# ── Sync de metadatos ──────────────────────────────────────────────────────────

def _sync_rows_responder(probe_tipo="mixto", orphans=None):
    orphans = orphans or []

    def responder(q: Q):
        if q.update_payload is not None or q.upsert_payload is not None:
            return SimpleNamespace(data=[])
        if q.has_op("is_", "tdr_tipo_extraccion", "null"):
            # barrido de huérfanos (una página basta: < PAGE_DB corta)
            return SimpleNamespace(data=list(orphans))
        if q.has_op("select", "tdr_tipo_extraccion") and q.has_op("eq", "id"):
            return SimpleNamespace(data=[{"tdr_tipo_extraccion": probe_tipo}])
        return SimpleNamespace(data=[{"ok": True}])

    return responder


def test_sync_meta_escribe_todos_y_clasifica_huerfanos(meta_log):
    meta_log.write_text(
        "\n".join([
            json.dumps({"id": 1, "tdr_tipo_extraccion": "mixto",
                        "paginas_ocr_pendientes": [2]}),
            json.dumps({"id": 2, "tdr_tipo_extraccion": "nativo_puro"}),
        ]) + "\n",
        encoding="utf-8",
    )
    texto_nativo = "A" * 200  # chars_utiles >= MIN_CHARS_PAGINA
    orphans = [
        {"id": 50, "tdr_texto": texto_nativo, "pdf_es_imagen": True,
         "tdr_tipo_extraccion": None, "req_url": "x"},
        {"id": 51, "tdr_texto": "marca (ocr) resto", "pdf_es_imagen": True,
         "tdr_tipo_extraccion": None, "req_url": "x"},
        {"id": 52, "tdr_texto": texto_nativo, "pdf_es_imagen": True,
         "tdr_tipo_extraccion": None, "req_url": "sin_pdf"},
        {"id": 1, "tdr_texto": texto_nativo, "pdf_es_imagen": True,
         "tdr_tipo_extraccion": None, "req_url": "x"},  # ya en jsonl
    ]
    supa = FakeSupa(_sync_rows_responder(orphans=orphans))

    n = dr.sync_meta_jsonl(supa)

    assert n == 3  # probe + 1 restante + 1 huérfano
    written_ids = sorted(
        op[1][1] for q in supa.updates() for op in q.ops if op[0] == "eq"
    )
    assert written_ids == [1, 2, 50]
    huerfano = next(q for q in supa.updates()
                    if q.update_payload.get("tdr_tipo_extraccion")
                    == "nativo_puro"
                    and q.update_payload.get("pdf_es_imagen") is False)
    assert huerfano.update_payload["paginas_ocr_pendientes"] == []


def test_sync_meta_sin_columnas_aborta(meta_log):
    supa = FakeSupa(lambda q: (_ for _ in ()).throw(RuntimeError("no cols")))

    with pytest.raises(SystemExit, match="faltan columnas"):
        dr.sync_meta_jsonl(supa)


def test_sync_meta_probe_no_persiste_aborta(meta_log):
    meta_log.write_text(
        json.dumps({"id": 1, "tdr_tipo_extraccion": "mixto"}) + "\n",
        encoding="utf-8",
    )
    supa = FakeSupa(_sync_rows_responder(probe_tipo=None))

    with pytest.raises(SystemExit, match="no persistió"):
        dr.sync_meta_jsonl(supa)


def test_sync_meta_aborta_tras_5_fallos(meta_log):
    meta_log.write_text(
        "\n".join(
            json.dumps({"id": i, "tdr_tipo_extraccion": "nativo_puro"})
            for i in range(1, 9)
        ) + "\n",
        encoding="utf-8",
    )
    calls = {"updates": 0}
    probe_ok = {"done": False}

    def responder(q: Q):
        if q.update_payload is not None:
            calls["updates"] += 1
            if not probe_ok["done"]:
                probe_ok["done"] = True
                return SimpleNamespace(data=[])
            raise RuntimeError("fallo sync")
        if q.has_op("is_", "tdr_tipo_extraccion", "null"):
            return SimpleNamespace(data=[])
        if q.has_op("select", "tdr_tipo_extraccion") and q.has_op("eq", "id"):
            return SimpleNamespace(data=[{"tdr_tipo_extraccion": "nativo_puro"}])
        return SimpleNamespace(data=[{"ok": True}])

    with pytest.raises(SystemExit, match="demasiados fallos"):
        dr.sync_meta_jsonl(FakeSupa(responder))

    assert calls["updates"] == 6  # probe + 5 fallos


def test_sync_meta_sin_jsonl_devuelve_cero(meta_log):
    supa = FakeSupa(lambda q: SimpleNamespace(data=[{"ok": True}]))
    assert dr.sync_meta_jsonl(supa) == 0


# ── Reportes ───────────────────────────────────────────────────────────────────

def test_reporte_extraccion_agrega_tipos_y_paginas():
    rows = [
        {"tdr_tipo_extraccion": "nativo_puro", "tdr_n_paginas": 2,
         "tdr_n_paginas_nativas": 2, "tdr_n_paginas_ocr": 0, "req_url": "u"},
        {"tdr_tipo_extraccion": "mixto", "tdr_n_paginas": 4,
         "tdr_n_paginas_nativas": 3, "tdr_n_paginas_ocr": 1, "req_url": "u"},
        {"tdr_tipo_extraccion": None, "tdr_n_paginas": 1,
         "tdr_n_paginas_nativas": None, "tdr_n_paginas_ocr": None,
         "req_url": "sin_pdf"},
        {"tdr_tipo_extraccion": None, "tdr_n_paginas": None,
         "tdr_n_paginas_nativas": None, "tdr_n_paginas_ocr": None,
         "req_url": "pendiente_ocr"},
        {"tdr_tipo_extraccion": None, "tdr_n_paginas": None,
         "tdr_n_paginas_nativas": None, "tdr_n_paginas_ocr": None,
         "req_url": "u"},
    ]
    supa = FakeSupa(lambda q: SimpleNamespace(data=list(rows)))

    rep = dr.reporte_extraccion(supa)

    assert rep == {
        "nativo_puro": 1, "mixto": 1, "imagen_total": 0, "sin_pdf": 1,
        "pendiente_ocr_viejo": 1, "sin_tipo": 2,
        "paginas_ocr_reales": 1, "paginas_totales": 6, "paginas_nativas": 5,
    }
    assert supa.queries[0].has_op("eq", "estado", "Vigente")
    assert supa.queries[0].has_op("order", "id")


def test_conteo_pdf_usa_count_exact():
    def responder(q: Q):
        if q.has_op("eq", "req_url", "pendiente_ocr"):
            return SimpleNamespace(count=3)
        if q.has_op("or_", "pdf_descargado.eq.false,pdf_descargado.is.null"):
            return SimpleNamespace(count=7)
        if q.has_op("eq", "pdf_descargado", True):
            return SimpleNamespace(count=9)
        return SimpleNamespace(count=11)

    counts = dr.conteo_pdf(FakeSupa(responder))

    assert counts == {
        "vigentes": 11, "pendientes": 7,
        "pendiente_ocr": 3, "ya_descargados": 9,
    }


def test_group_by_tipo_cuenta_null_y_respeta_vigentes():
    rows = [
        {"tdr_tipo_extraccion": "mixto"},
        {"tdr_tipo_extraccion": "mixto"},
        {"tdr_tipo_extraccion": None},
    ]
    supa = FakeSupa(lambda q: SimpleNamespace(data=list(rows)))

    gb = dr.group_by_tipo(supa, vigentes=True)

    assert gb == {"mixto": 2, "NULL": 1}
    assert supa.queries[0].has_op("eq", "estado", "Vigente")


def test_columnas_extraccion_ok_fail_soft():
    assert dr.columnas_extraccion_ok(
        FakeSupa(lambda q: SimpleNamespace(data=[{"x": 1}]))
    )
    assert not dr.columnas_extraccion_ok(
        FakeSupa(lambda q: (_ for _ in ()).throw(RuntimeError("boom")))
    )


# ── Persistencia documental ────────────────────────────────────────────────────

def test_guardar_ok_payload_y_meta(meta_log):
    supa = FakeSupa()
    dr.guardar_ok(supa, {
        "id": 42, "tdr_texto": "texto", "pdf_hash": "abc",
        "n_paginas": 4, "ocr_paginas": [2], "ocr_hechas": [2],
        "n_paginas_ocr": 1, "n_paginas_nativas": 3,
        "url": "http://req", "tdr_tipo_extraccion": "mixto",
        "pdf_archivo_id": 9, "pdf_nombre": "tdr.pdf",
        "pdf_storage_path": "tdr/42.pdf", "pdf_storage_bytes": 10,
    })

    q = supa.updates()[0]
    p = q.update_payload
    assert q.has_op("eq", "id", 42)
    assert p["pdf_descargado"] is True
    assert p["pdf_procesado"] is True
    assert p["pdf_es_imagen"] is True
    assert p["paginas_ocr_pendientes"] == [2]
    assert p["pdf_storage_path"] == "tdr/42.pdf"
    assert "pdf_storage_at" in p
    # update_contrato traduce los marcadores privados a columnas reales
    assert p["pdf_archivo_id"] == 9
    assert p["pdf_nombre"] == "tdr.pdf"
    # meta local también escribió
    assert dr.meta_local_por_id()[42]["tdr_tipo_extraccion"] == "mixto"


def test_guardar_pendiente_ocr_marca_descarga_falsa(meta_log):
    supa = FakeSupa()
    dr.guardar_pendiente_ocr(supa, 5, {
        "pdf_hash": "h", "pdf_archivo_id": 1, "pdf_nombre": "a.pdf",
    })

    p = supa.updates()[0].update_payload
    assert p["req_url"] == "pendiente_ocr"
    assert p["pdf_descargado"] is False
    assert p["pdf_es_imagen"] is True
    assert p["pdf_archivo_id"] == 1
    assert p["pdf_nombre"] == "a.pdf"


def test_guardar_sin_pdf_cierra_sin_tdr():
    supa = FakeSupa()
    dr.guardar_sin_pdf(supa, 5)

    p = supa.updates()[0].update_payload
    assert p["req_url"] == "sin_pdf"
    assert p["pdf_descargado"] is True
    assert p["pdf_procesado"] is True
    assert p["tdr_texto"] is None


def test_persistir_storage_si_hay_solo_con_path():
    supa = FakeSupa()
    dr.persistir_storage_si_hay(supa, 5, {})
    assert supa.updates() == []

    dr.persistir_storage_si_hay(supa, 5, {
        "pdf_storage_path": "tdr/5.pdf", "pdf_storage_bytes": 3,
        "pdf_archivo_id": 8,
    })
    p = supa.updates()[0].update_payload
    assert p["pdf_storage_path"] == "tdr/5.pdf"
    assert p["pdf_storage_bytes"] == 3
    assert p["pdf_archivo_id"] == 8


def test_guardar_ocr_progreso_meta_y_contrato(meta_log):
    supa = FakeSupa()
    dr.guardar_ocr_progreso(supa, {
        "id": 9, "tdr_n_paginas": 6,
        "pdf_archivo_id": 3, "pdf_nombre": "scan.pdf",
    }, "tdr texto", [4], [1, 2])

    p = supa.updates()[0].update_payload
    assert p["paginas_ocr_pendientes"] == [4]
    assert p["paginas_ocr_hechas"] == [1, 2]
    assert p["pdf_es_imagen"] is True
    assert p["pdf_descargado"] is True
    assert p["tdr_tipo_extraccion"] == "mixto"  # clasificado por páginas
    assert p["pdf_archivo_id"] == 3
    rec = dr.meta_local_por_id()[9]
    assert rec["paginas_ocr_hechas"] == [1, 2]


def test_payload_rechazo_proyeccion_contrato():
    payload = dr.payload_rechazo(
        {"id": 7, "nro_contratacion": "N1",
         "descripcion_contrato": "desc"},
        "motivo", {"extra": 1},
    )
    assert payload == {
        "idContrato": 7, "nroContratacion": "N1",
        "desContratacion": "desc", "motivo": "motivo", "extra": 1,
    }


# ── Elegibilidad fresca y cola ────────────────────────────────────────────────

def _fila_vigente(**kw):
    row = {
        "id": 1, "estado": "Vigente",
        "fecha_ini_cotizacion": "2026-09-20T00:00:00Z",
        "fecha_fin_cotizacion": "2099-01-01T00:00:00Z",
        "categoria_it": "software", "relevancia_ia": None,
    }
    row.update(kw)
    return row


def test_elegible_estados_y_ventana():
    def supa_con(row):
        return FakeSupa(lambda q: SimpleNamespace(data=[row] if row else []))

    assert dr.contrato_ocr_sigue_elegible(
        supa_con(None), 1, solo_ti=False) == (False, "no_encontrado")
    ok, razon = dr.contrato_ocr_sigue_elegible(
        supa_con(_fila_vigente(estado="Caducado")), 1, solo_ti=False)
    assert (ok, razon) == (False, "estado=Caducado")
    ok, razon = dr.contrato_ocr_sigue_elegible(
        supa_con(_fila_vigente(fecha_fin_cotizacion=None)), 1, solo_ti=False)
    assert (ok, razon) == (False, "ventana_null")
    ok, razon = dr.contrato_ocr_sigue_elegible(
        supa_con(_fila_vigente(fecha_fin_cotizacion="2000-01-01T00:00:00Z")),
        1, solo_ti=False)
    assert (ok, razon) == (False, "vencido")
    ok, razon = dr.contrato_ocr_sigue_elegible(
        supa_con(_fila_vigente(fecha_ini_cotizacion="2099-01-01T00:00:00Z")),
        1, solo_ti=False)
    assert (ok, razon) == (False, "por_abrir")
    ok, razon = dr.contrato_ocr_sigue_elegible(
        supa_con(_fila_vigente(categoria_it=None, relevancia_ia=None)),
        1, solo_ti=True)
    assert (ok, razon) == (False, "no_ti")
    assert dr.contrato_ocr_sigue_elegible(
        supa_con(_fila_vigente()), 1, solo_ti=True) == (True, "ok")


def test_pendientes_ocr_vacio_sin_cola(meta_log):
    supa = FakeSupa(
        lambda q: SimpleNamespace(data=[{"ok": True}])
        if q.has_op("limit") else SimpleNamespace(data=[])
    )

    filas, stats = dr.pendientes_ocr_paginas(supa, 10)

    assert filas == []
    assert stats["crudos"] == 0


def test_pendientes_ocr_fusiona_local_y_restituye_hechas(meta_log):
    meta_log.write_text(json.dumps({
        "id": 9, "paginas_ocr_pendientes": [1, 2],
        "paginas_ocr_hechas": [1], "tdr_tipo_extraccion": "mixto",
        "tdr_n_paginas": 4, "tdr_n_paginas_nativas": 2, "tdr_n_paginas_ocr": 2,
    }) + "\n", encoding="utf-8")

    def responder(q: Q):
        ops = {op[0] for op in q.ops}
        if q.update_payload is not None:
            return SimpleNamespace(data=[])
        select_arg = next(
            (op[1][0] for op in q.ops if op[0] == "select"), "")
        if "paginas_ocr_pendientes" in select_arg:
            # sondeo de columnas + barrido principal → vacío
            if q.has_op("limit"):
                return SimpleNamespace(data=[{"ok": True}])
            return SimpleNamespace(data=[])
        if q.has_op("in_", "id"):
            if "tdr_texto" in select_arg:
                return SimpleNamespace(data=[{
                    "id": 9, "tdr_texto": "t", "pdf_archivo_id": 1,
                    "pdf_nombre": "a.pdf", "pdf_es_imagen": True,
                    "estado": "Vigente",
                    "fecha_fin_cotizacion": "2099-01-01T00:00:00Z",
                }])
            return SimpleNamespace(data=[{"id": 9}])
        return SimpleNamespace(data=[])

    supa = FakeSupa(responder)
    filas, stats = dr.pendientes_ocr_paginas(
        supa, 10, incluir_por_abrir=True)

    assert len(filas) == 1
    assert filas[0]["paginas_ocr_pendientes"] == [2]  # 1 ya hecha se descarta
    assert filas[0]["paginas_ocr_hechas"] == [1]
    assert stats["ok"] == 1


# ── run_ocr_selectivo: salidas tempranas ──────────────────────────────────────

@pytest.fixture
def selectivo_doubles(tmp_path, monkeypatch):
    """Neutraliza efectos de run_ocr_selectivo y captura el log final."""
    captured: dict = {}

    monkeypatch.setattr(dr, "OCR_LOG", tmp_path / "ultima_ocr.txt")
    monkeypatch.setattr(
        dr, "escribir_ocr_log",
        lambda supa, stats: captured.update(stats),
    )
    monkeypatch.setattr(
        dr, "escribir_resumen", lambda supa, stats: None)
    monkeypatch.setattr(dr, "columnas_extraccion_ok", lambda supa: True)
    monkeypatch.setattr(dr, "SeaceHttp",
                        lambda headed=False: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(dr.time, "sleep", lambda s: None)
    return captured


def test_selectivo_cupo_alcanzado_no_consulta_cola(
        selectivo_doubles, monkeypatch):
    monkeypatch.setattr(dr, "cargar_cuota_ocr", lambda supa: {
        "fecha": "2026-09-26", "requests": 6000, "usd_est": 1.0,
    })
    llamado = []
    monkeypatch.setattr(dr, "pendientes_ocr_paginas",
                        lambda *a, **kw: llamado.append(1) or ([], {}))

    dr.run_ocr_selectivo(FakeSupa(), limit=0, ids=[], headed=False,
                         max_dia=6000, dry_run=False)

    assert llamado == []  # no construyó la cola
    assert selectivo_doubles["motivo_parada"] == "cupo"
    assert selectivo_doubles["exit"] == 0


def test_selectivo_dry_run_solo_reporta_cola(selectivo_doubles, monkeypatch):
    monkeypatch.setattr(dr, "cargar_cuota_ocr", lambda supa: {
        "fecha": "2026-09-26", "requests": 0, "usd_est": 0.0,
    })
    fila = {"id": 1, "paginas_ocr_pendientes": [2, 3]}
    monkeypatch.setattr(dr, "pendientes_ocr_paginas",
                        lambda *a, **kw: ([fila], {"alta": 1}))
    ocr_calls = []
    monkeypatch.setattr(dr, "ocr_contrato_selectivo",
                        lambda *a, **kw: ocr_calls.append(1))

    dr.run_ocr_selectivo(FakeSupa(), limit=10, ids=[], headed=False,
                         max_dia=6000, dry_run=True)

    assert ocr_calls == []
    assert selectivo_doubles["motivo_parada"] == "dry_run"
    assert selectivo_doubles["cola_paginas"] == 2


def test_selectivo_cola_vacia_es_idempotente(selectivo_doubles, monkeypatch):
    monkeypatch.setattr(dr, "cargar_cuota_ocr", lambda supa: {
        "fecha": "2026-09-26", "requests": 0, "usd_est": 0.0,
    })
    monkeypatch.setattr(dr, "pendientes_ocr_paginas",
                        lambda *a, **kw: ([], {"ok": 0}))

    dr.run_ocr_selectivo(FakeSupa(), limit=10, ids=[], headed=False,
                         max_dia=6000, dry_run=False)

    assert selectivo_doubles["motivo_parada"] == "vacio"
    assert selectivo_doubles["pendientes_contratos"] == 0


def test_selectivo_ids_restringe_la_cola(selectivo_doubles, monkeypatch):
    monkeypatch.setattr(dr, "cargar_cuota_ocr", lambda supa: {
        "fecha": "x", "requests": 0, "usd_est": 0.0,
    })
    filas = [{"id": 1, "paginas_ocr_pendientes": [1]},
             {"id": 2, "paginas_ocr_pendientes": [1]}]
    monkeypatch.setattr(dr, "pendientes_ocr_paginas",
                        lambda *a, **kw: (list(filas), {}))
    procesados = []
    monkeypatch.setattr(dr, "contrato_ocr_sigue_elegible",
                        lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(dr, "ocr_contrato_selectivo", lambda *a, **kw: (
        procesados.append(1) or {"nuevas": [1], "pend": []}))
    monkeypatch.setattr(dr, "rechunk_embed_pdf", lambda *a: None)
    restante = []
    monkeypatch.setattr(dr, "pendientes_ocr_paginas", lambda *a, **kw: (
        (list(filas), {}) if not procesados else (restante, {})))

    dr.run_ocr_selectivo(FakeSupa(), limit=0, ids=[2], headed=False,
                         max_dia=6000, dry_run=False)

    assert len(procesados) == 1  # solo id=2 entró
    assert selectivo_doubles["ids_tocados"] == "2"


def test_selectivo_error_registra_rechazo_y_sigue(
        selectivo_doubles, monkeypatch):
    monkeypatch.setattr(dr, "cargar_cuota_ocr", lambda supa: {
        "fecha": "x", "requests": 0, "usd_est": 0.0,
    })
    filas = [{"id": 1, "paginas_ocr_pendientes": [1]},
             {"id": 2, "paginas_ocr_pendientes": [1]}]
    colas = iter([(list(filas), {}), ([], {})])
    monkeypatch.setattr(dr, "pendientes_ocr_paginas",
                        lambda *a, **kw: next(colas))
    monkeypatch.setattr(dr, "contrato_ocr_sigue_elegible",
                        lambda *a, **kw: (True, "ok"))

    def falla(http, supa, c, cuota, max_dia, **kw):
        if int(c["id"]) == 1:
            raise RuntimeError("boom")
        return {"nuevas": [], "pend": []}

    monkeypatch.setattr(dr, "ocr_contrato_selectivo", falla)
    rechazos = []
    monkeypatch.setattr(dr, "registrar_rechazo",
                        lambda supa, payload, motivo, origen=None:
                        rechazos.append(payload))
    monkeypatch.setattr(dr, "rechunk_embed_pdf", lambda *a: None)

    dr.run_ocr_selectivo(FakeSupa(), limit=0, ids=[], headed=False,
                         max_dia=6000, dry_run=False)

    assert selectivo_doubles["err"] == 1
    assert selectivo_doubles["motivo_parada"] == "completo"
    assert rechazos and rechazos[0]["idContrato"] == 1


def test_selectivo_cupo_a_media_detiene_y_rechunk(selectivo_doubles,
                                                 monkeypatch):
    monkeypatch.setattr(dr, "cargar_cuota_ocr", lambda supa: {
        "fecha": "x", "requests": 0, "usd_est": 0.0,
    })
    filas = [{"id": 1, "paginas_ocr_pendientes": [1]},
             {"id": 2, "paginas_ocr_pendientes": [1]}]
    colas = iter([(list(filas), {}), ([], {})])
    monkeypatch.setattr(dr, "pendientes_ocr_paginas",
                        lambda *a, **kw: next(colas))
    monkeypatch.setattr(dr, "contrato_ocr_sigue_elegible",
                        lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(dr, "ocr_contrato_selectivo",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            dr.CupoFlash("tope", motivo="cupo")))
    rechunked = []
    monkeypatch.setattr(dr, "rechunk_embed_pdf",
                        lambda supa, cid: rechunked.append(cid))

    dr.run_ocr_selectivo(FakeSupa(), limit=0, ids=[], headed=False,
                         max_dia=6000, dry_run=False)

    assert selectivo_doubles["motivo_parada"] == "cupo"
    assert rechunked == [1]  # entró a OCR → rechunk parcial
    assert selectivo_doubles["ocr_contratos"] == 0


def test_respetar_rpm_sin_limite_no_duerme(monkeypatch):
    sleeps = []
    monkeypatch.setattr(dr.time, "sleep", sleeps.append)
    monkeypatch.setattr(dr, "OCR_RPM", 0.0)
    monkeypatch.setattr(dr, "_OCR_NEXT", 0.0)
    dr.respetar_rpm()
    assert sleeps == []


def test_respetar_rpm_espera_hasta_la_ventana(monkeypatch):
    sleeps = []
    monkeypatch.setattr(dr.time, "sleep", sleeps.append)
    monkeypatch.setattr(dr, "OCR_RPM", 60.0)
    monkeypatch.setattr(dr, "_OCR_NEXT", dr.time.time() + 5)
    dr.respetar_rpm()
    assert len(sleeps) == 1
    assert 4 < sleeps[0] <= 5
    assert dr._OCR_NEXT > dr.time.time()
