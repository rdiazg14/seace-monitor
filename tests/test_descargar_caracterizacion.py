"""Caracterización del flujo ``descargar_requerimiento``.

Cubre con dobles el comportamiento observado: sidecar JSONL (última línea por
id gana), cuota OCR (BD → fallback local → reinicio diario), sync de metadatos
(probe, abortos y huérfanos), reportes, persistencia documental y las salidas
del OCR selectivo. Nada toca red ni servicios reales: reloj, rutas, repositorio
y colaboradores se inyectan.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import descargar_requerimiento as dr
from seace_monitor.documents import meta, persistencia, reportes
from seace_monitor.documents import pdf_extraction
from seace_monitor.documents.seace_files import PdfTruncado
from seace_monitor.ingestion import repository as ingestion_repository
from seace_monitor.ocr import cuota as ocr_cuota
from seace_monitor.ocr import pendientes as ocr_pendientes
from seace_monitor.ocr import selectivo


HOY = "2026-09-26"


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

    def neq(self, *a):
        return self._op("neq", *a)

    def gte(self, *a):
        return self._op("gte", *a)

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
def meta_log(tmp_path):
    return tmp_path / "tdr_extraccion.jsonl"


@pytest.fixture
def cuota_path(tmp_path):
    return tmp_path / "flash_ocr_cuota.json"


# ── Sidecar JSONL ─────────────────────────────────────────────────────────────

def test_meta_local_ultima_linea_por_id_gana(meta_log):
    meta.registrar_meta_local(
        {"id": 1, "n_paginas": 3, "ocr_paginas": [2]}, path=meta_log)
    meta.registrar_meta_local({
        "id": 1,
        "tdr_tipo_extraccion": "imagen_total",
        "ocr_paginas": [1, 2, 3],
        "n_paginas": 3,
    }, path=meta_log)
    meta.registrar_meta_local(
        {"id": 2, "n_paginas": 5, "ocr_paginas": []}, path=meta_log)

    by_id = meta.meta_local_por_id(path=meta_log)

    assert by_id[1]["tdr_tipo_extraccion"] == "imagen_total"
    assert by_id[1]["paginas_ocr_pendientes"] == [1, 2, 3]
    assert by_id[2]["tdr_tipo_extraccion"] == "nativo_puro"
    assert by_id[2]["pdf_es_imagen"] is False


def test_registrar_meta_local_deriva_campos(meta_log):
    meta.registrar_meta_local({
        "id": 7, "n_paginas": 4, "ocr_paginas": [2, 4],
        "ocr_hechas": [2], "n_paginas_nativas": 2, "n_paginas_ocr": 2,
        "chars_final": 1234,
    }, path=meta_log)

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
    assert meta.reporte_jsonl(path=meta_log) == {
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

    assert meta.reporte_jsonl(path=meta_log) == {
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

    payload = meta.payload_extraccion(rec)

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
    assert meta.payload_extraccion({})["pdf_es_imagen"] is None


# ── Cuota OCR ─────────────────────────────────────────────────────────────────

def test_cuota_remota_gana_sobre_local(cuota_path):
    cuota_path.write_text(json.dumps({
        "fecha": HOY, "requests": 99,
    }), encoding="utf-8")
    supa = FakeSupa(lambda q: SimpleNamespace(data={
        "requests": 5, "prompt_tokens": 10, "out_tokens": 4, "usd_est": 0.01,
    }))

    cuota = ocr_cuota.cargar_cuota_ocr(supa, hoy=HOY, path=cuota_path)

    assert cuota == {
        "fecha": HOY, "requests": 5, "prompt_tokens": 10,
        "out_tokens": 4, "usd_est": 0.01,
    }
    assert supa.queries[0].table_name == "pipeline_cuota_ocr"
    assert supa.queries[0].has_op("eq", "fecha_lima", HOY)
    assert supa.queries[0].has_op("maybe_single")


def test_cuota_falla_remota_cae_al_local(cuota_path):
    cuota_path.write_text(json.dumps({
        "fecha": HOY, "requests": 3, "usd_est": 0.02,
    }), encoding="utf-8")

    def falla(q):
        raise RuntimeError("PostgREST down")

    cuota = ocr_cuota.cargar_cuota_ocr(
        FakeSupa(falla), hoy=HOY, path=cuota_path)

    assert cuota["requests"] == 3
    assert cuota["prompt_tokens"] == 0  # setdefault rellena contadores
    assert cuota["usd_est"] == 0.02


def test_cuota_reinicia_con_fecha_distinta(cuota_path):
    cuota_path.write_text(json.dumps({
        "fecha": "2026-09-25", "requests": 4000,
    }), encoding="utf-8")

    cuota = ocr_cuota.cargar_cuota_ocr(
        FakeSupa(), hoy=HOY, path=cuota_path)

    assert cuota == {
        "fecha": HOY, "requests": 0, "prompt_tokens": 0,
        "out_tokens": 0, "usd_est": 0.0,
    }


def test_cuota_sin_fuentes_arranca_en_cero(cuota_path):
    assert ocr_cuota.cargar_cuota_ocr(
        FakeSupa(), hoy=HOY, path=cuota_path)["requests"] == 0
    assert ocr_cuota.cargar_cuota_ocr(
        None, hoy=HOY, path=cuota_path)["fecha"] == HOY


def test_guardar_cuota_upsert_remoto_y_siempre_local(cuota_path):
    supa = FakeSupa()

    ocr_cuota.guardar_cuota_ocr(supa, {
        "fecha": HOY, "requests": 2, "prompt_tokens": 5,
        "out_tokens": 3, "usd_est": 0.001,
    }, path=cuota_path)

    q = supa.queries[0]
    assert q.table_name == "pipeline_cuota_ocr"
    assert q.upsert_payload["fecha_lima"] == HOY
    assert q.upsert_payload["requests"] == 2
    assert "updated_at" in q.upsert_payload
    assert q.upsert_kw == {"on_conflict": "fecha_lima"}
    assert json.loads(cuota_path.read_text(encoding="utf-8"))["requests"] == 2


def test_guardar_cuota_falla_remota_igual_escribe_local(cuota_path):
    def falla(q):
        raise RuntimeError("sin tabla")

    ocr_cuota.guardar_cuota_ocr(
        FakeSupa(falla), {"fecha": "x", "requests": 1}, path=cuota_path)

    assert json.loads(cuota_path.read_text(encoding="utf-8"))["requests"] == 1


def test_registrar_ocr_ok_acumula_tokens_uso_ia_y_tope(cuota_path):
    supa = FakeSupa()
    cuota = {"fecha": HOY, "requests": 0,
             "prompt_tokens": 0, "out_tokens": 0, "usd_est": 0.0}

    with pytest.raises(dr.CupoFlash):
        ocr_cuota.registrar_ocr_ok(
            supa, cuota, 1,
            last_usage={"prompt": 100, "candidates": 20},
            path=cuota_path,
        )

    assert cuota["requests"] == 1
    assert cuota["prompt_tokens"] == 100
    assert cuota["out_tokens"] == 20
    assert cuota["usd_est"] > 0
    uso = supa.inserts("uso_ia")
    assert len(uso) == 1
    assert uso[0].insert_payload["componente"] == "ocr"
    assert uso[0].insert_payload["tokens_total"] == 120


def test_registrar_ocr_ok_bajo_tope_no_lanza(cuota_path):
    cuota = {"fecha": HOY, "requests": 0}

    ocr_cuota.registrar_ocr_ok(
        None, cuota, 6000,
        last_usage={"prompt": 1, "candidates": 1},
        path=cuota_path,
    )

    assert cuota["requests"] == 1


# ── Anexar / helpers puros ─────────────────────────────────────────────────────

def test_anexar_ocr_a_tdr_idempotente_y_marca():
    anexar = pdf_extraction.anexar_ocr_a_tdr
    assert anexar("", 2, "texto") == "--- pagina 2 (ocr) ---\ntexto"
    base = "nativo"
    out = anexar(base, 3, "  ocr  ")
    assert out == "nativo\n\n--- pagina 3 (ocr) ---\nocr"
    # misma marca ya presente → no duplica
    assert anexar(out, 3, "otro") == out


def test_as_int_list_tolerante():
    assert ocr_pendientes._as_int_list(None) == []
    assert ocr_pendientes._as_int_list("[1, 2]") == [1, 2]
    assert ocr_pendientes._as_int_list(["3", "x", 4]) == [3, 4]


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

    n = meta.sync_meta_jsonl(supa, path=meta_log)

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
        meta.sync_meta_jsonl(supa, path=meta_log)


def test_sync_meta_probe_no_persiste_aborta(meta_log):
    meta_log.write_text(
        json.dumps({"id": 1, "tdr_tipo_extraccion": "mixto"}) + "\n",
        encoding="utf-8",
    )
    supa = FakeSupa(_sync_rows_responder(probe_tipo=None))

    with pytest.raises(SystemExit, match="no persistió"):
        meta.sync_meta_jsonl(supa, path=meta_log)


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
        meta.sync_meta_jsonl(FakeSupa(responder), path=meta_log)

    assert calls["updates"] == 6  # probe + 5 fallos


def test_sync_meta_sin_jsonl_devuelve_cero(meta_log):
    supa = FakeSupa(lambda q: SimpleNamespace(data=[{"ok": True}]))
    assert meta.sync_meta_jsonl(supa, path=meta_log) == 0


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

    rep = reportes.reporte_extraccion(supa)

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

    counts = reportes.conteo_pdf(FakeSupa(responder))

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

    gb = reportes.group_by_tipo(supa, vigentes=True)

    assert gb == {"mixto": 2, "NULL": 1}
    assert supa.queries[0].has_op("eq", "estado", "Vigente")


def test_columnas_extraccion_ok_fail_soft():
    assert meta.columnas_extraccion_ok(
        FakeSupa(lambda q: SimpleNamespace(data=[{"x": 1}]))
    )
    assert not meta.columnas_extraccion_ok(
        FakeSupa(lambda q: (_ for _ in ()).throw(RuntimeError("boom")))
    )


# ── Persistencia documental ────────────────────────────────────────────────────

def test_guardar_ok_payload_y_meta(meta_log):
    supa = FakeSupa()
    persistencia.guardar_ok(supa, {
        "id": 42, "tdr_texto": "texto", "pdf_hash": "abc",
        "n_paginas": 4, "ocr_paginas": [2], "ocr_hechas": [2],
        "n_paginas_ocr": 1, "n_paginas_nativas": 3,
        "url": "http://req", "tdr_tipo_extraccion": "mixto",
        "pdf_archivo_id": 9, "pdf_nombre": "tdr.pdf",
        "pdf_storage_path": "tdr/42.pdf", "pdf_storage_bytes": 10,
    }, meta_path=meta_log)

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
    assert meta.meta_local_por_id(
        path=meta_log)[42]["tdr_tipo_extraccion"] == "mixto"


def test_guardar_pendiente_ocr_marca_descarga_falsa(meta_log):
    supa = FakeSupa()
    persistencia.guardar_pendiente_ocr(supa, 5, {
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
    persistencia.guardar_sin_pdf(supa, 5)

    p = supa.updates()[0].update_payload
    assert p["req_url"] == "sin_pdf"
    assert p["pdf_descargado"] is True
    assert p["pdf_procesado"] is True
    assert p["tdr_texto"] is None


def test_persistir_storage_si_hay_solo_con_path():
    supa = FakeSupa()
    persistencia.persistir_storage_si_hay(supa, 5, {})
    assert supa.updates() == []

    persistencia.persistir_storage_si_hay(supa, 5, {
        "pdf_storage_path": "tdr/5.pdf", "pdf_storage_bytes": 3,
        "pdf_archivo_id": 8,
    })
    p = supa.updates()[0].update_payload
    assert p["pdf_storage_path"] == "tdr/5.pdf"
    assert p["pdf_storage_bytes"] == 3
    assert p["pdf_archivo_id"] == 8


def test_guardar_ocr_progreso_meta_y_contrato(meta_log):
    supa = FakeSupa()
    persistencia.guardar_ocr_progreso(supa, {
        "id": 9, "tdr_n_paginas": 6,
        "pdf_archivo_id": 3, "pdf_nombre": "scan.pdf",
    }, "tdr texto", [4], [1, 2], meta_path=meta_log)

    p = supa.updates()[0].update_payload
    assert p["paginas_ocr_pendientes"] == [4]
    assert p["paginas_ocr_hechas"] == [1, 2]
    assert p["pdf_es_imagen"] is True
    assert p["pdf_descargado"] is True
    assert p["tdr_tipo_extraccion"] == "mixto"  # clasificado por páginas
    assert p["pdf_archivo_id"] == 3
    rec = meta.meta_local_por_id(path=meta_log)[9]
    assert rec["paginas_ocr_hechas"] == [1, 2]


def test_payload_rechazo_proyeccion_contrato():
    payload = ingestion_repository.payload_rechazo(
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

    elegible = ocr_pendientes.contrato_ocr_sigue_elegible
    assert elegible(
        supa_con(None), 1, solo_ti=False) == (False, "no_encontrado")
    ok, razon = elegible(
        supa_con(_fila_vigente(estado="Caducado")), 1, solo_ti=False)
    assert (ok, razon) == (False, "estado=Caducado")
    ok, razon = elegible(
        supa_con(_fila_vigente(fecha_fin_cotizacion=None)), 1, solo_ti=False)
    assert (ok, razon) == (False, "ventana_null")
    ok, razon = elegible(
        supa_con(_fila_vigente(fecha_fin_cotizacion="2000-01-01T00:00:00Z")),
        1, solo_ti=False)
    assert (ok, razon) == (False, "vencido")
    ok, razon = elegible(
        supa_con(_fila_vigente(fecha_ini_cotizacion="2099-01-01T00:00:00Z")),
        1, solo_ti=False)
    assert (ok, razon) == (False, "por_abrir")
    ok, razon = elegible(
        supa_con(_fila_vigente(categoria_it=None, relevancia_ia=None)),
        1, solo_ti=True)
    assert (ok, razon) == (False, "no_ti")
    assert elegible(
        supa_con(_fila_vigente()), 1, solo_ti=True) == (True, "ok")


def test_pendientes_ocr_vacio_sin_cola(meta_log):
    supa = FakeSupa(
        lambda q: SimpleNamespace(data=[{"ok": True}])
        if q.has_op("limit") else SimpleNamespace(data=[])
    )

    filas, stats = ocr_pendientes.pendientes_ocr_paginas(
        supa, 10, meta_path=meta_log)

    assert filas == []
    assert stats["crudos"] == 0


def test_pendientes_ocr_fusiona_local_y_restituye_hechas(meta_log):
    meta_log.write_text(json.dumps({
        "id": 9, "paginas_ocr_pendientes": [1, 2],
        "paginas_ocr_hechas": [1], "tdr_tipo_extraccion": "mixto",
        "tdr_n_paginas": 4, "tdr_n_paginas_nativas": 2, "tdr_n_paginas_ocr": 2,
    }) + "\n", encoding="utf-8")

    def responder(q: Q):
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
    filas, stats = ocr_pendientes.pendientes_ocr_paginas(
        supa, 10, incluir_por_abrir=True, meta_path=meta_log)

    assert len(filas) == 1
    assert filas[0]["paginas_ocr_pendientes"] == [2]  # 1 ya hecha se descarta
    assert filas[0]["paginas_ocr_hechas"] == [1]
    assert stats["ok"] == 1


# ── run_ocr_selectivo: salidas tempranas ──────────────────────────────────────

def _run_selectivo(supa, captured, **kw):
    """Ejecuta la corrida con todos los colaboradores simulados."""
    return selectivo.run_ocr_selectivo(
        supa,
        ocr_contrato=kw.pop("ocr_contrato",
                            lambda *a, **k: {"nuevas": [], "pend": []}),
        rechunk=kw.pop("rechunk", lambda supa_, cid: None),
        imprimir=kw.pop("imprimir", lambda *a: None),
        cargar_cuota=kw.pop("cargar_cuota", lambda s: {
            "fecha": HOY, "requests": 0, "usd_est": 0.0,
        }),
        pendientes=kw.pop("pendientes", lambda *a, **k: ([], {})),
        elegible=kw.pop("elegible", lambda *a, **k: (True, "ok")),
        columnas_ok=kw.pop("columnas_ok", lambda s: True),
        http_factory=kw.pop(
            "http_factory",
            lambda headed=False: SimpleNamespace(close=lambda: None)),
        rechazar=kw.pop("rechazar", lambda *a, **k: None),
        log_ocr=kw.pop("log_ocr", lambda s, stats: captured.update(stats)),
        log_resumen=kw.pop("log_resumen", lambda s, stats: None),
        sleep=kw.pop("sleep", lambda s: None),
        limit=kw.pop("limit", 0),
        ids=kw.pop("ids", []),
        headed=kw.pop("headed", False),
        max_dia=kw.pop("max_dia", 6000),
        dry_run=kw.pop("dry_run", False),
        **kw,
    )


def test_selectivo_cupo_alcanzado_no_consulta_cola():
    captured: dict = {}
    llamado = []

    _run_selectivo(
        FakeSupa(), captured,
        cargar_cuota=lambda s: {
            "fecha": HOY, "requests": 6000, "usd_est": 1.0},
        pendientes=lambda *a, **k: llamado.append(1) or ([], {}),
    )

    assert llamado == []  # no construyó la cola
    assert captured["motivo_parada"] == "cupo"
    assert captured["exit"] == 0


def test_selectivo_dry_run_solo_reporta_cola():
    captured: dict = {}
    fila = {"id": 1, "paginas_ocr_pendientes": [2, 3]}
    ocr_calls = []

    _run_selectivo(
        FakeSupa(), captured,
        pendientes=lambda *a, **k: ([fila], {"alta": 1}),
        ocr_contrato=lambda *a, **k: ocr_calls.append(1),
        dry_run=True, limit=10,
    )

    assert ocr_calls == []
    assert captured["motivo_parada"] == "dry_run"
    assert captured["cola_paginas"] == 2


def test_selectivo_cola_vacia_es_idempotente():
    captured: dict = {}

    _run_selectivo(
        FakeSupa(), captured,
        pendientes=lambda *a, **k: ([], {"ok": 0}),
        limit=10,
    )

    assert captured["motivo_parada"] == "vacio"
    assert captured["pendientes_contratos"] == 0


def test_selectivo_ids_restringe_la_cola():
    captured: dict = {}
    filas = [{"id": 1, "paginas_ocr_pendientes": [1]},
             {"id": 2, "paginas_ocr_pendientes": [1]}]
    procesados = []
    llamadas = iter([(list(filas), {}), ([], {})])

    _run_selectivo(
        FakeSupa(), captured,
        pendientes=lambda *a, **k: next(llamadas),
        ocr_contrato=lambda *a, **k: procesados.append(1) or {
            "nuevas": [1], "pend": []},
        ids=[2],
    )

    assert len(procesados) == 1  # solo id=2 entró
    assert captured["ids_tocados"] == "2"


def test_selectivo_error_registra_rechazo_y_sigue():
    captured: dict = {}
    filas = [{"id": 1, "paginas_ocr_pendientes": [1]},
             {"id": 2, "paginas_ocr_pendientes": [1]}]
    colas = iter([(list(filas), {}), ([], {})])
    rechazos = []

    def falla(http, supa, c, cuota, max_dia, **kw):
        if int(c["id"]) == 1:
            raise RuntimeError("boom")
        return {"nuevas": [], "pend": []}

    _run_selectivo(
        FakeSupa(), captured,
        pendientes=lambda *a, **k: next(colas),
        ocr_contrato=falla,
        rechazar=lambda supa, payload, motivo, origen=None:
            rechazos.append(payload),
    )

    assert captured["err"] == 1
    assert captured["motivo_parada"] == "completo"
    assert rechazos and rechazos[0]["idContrato"] == 1


def test_selectivo_cupo_a_media_detiene_y_rechunk():
    captured: dict = {}
    filas = [{"id": 1, "paginas_ocr_pendientes": [1]},
             {"id": 2, "paginas_ocr_pendientes": [1]}]
    colas = iter([(list(filas), {}), ([], {})])
    rechunked = []

    _run_selectivo(
        FakeSupa(), captured,
        pendientes=lambda *a, **k: next(colas),
        ocr_contrato=lambda *a, **k: (_ for _ in ()).throw(
            dr.CupoFlash("tope", motivo="cupo")),
        rechunk=lambda supa, cid: rechunked.append(cid),
    )

    assert captured["motivo_parada"] == "cupo"
    assert rechunked == [1]  # entró a OCR → rechunk parcial
    assert captured["ocr_contratos"] == 0


def test_escribir_ocr_log_archivo_pipeline_runs_y_summary(
        tmp_path, monkeypatch):
    supa = FakeSupa()
    log = tmp_path / "ultima_ocr.txt"
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    selectivo.escribir_ocr_log(supa, {
        "cola_contratos": 2, "cola_paginas": 5,
        "ocr_contratos": 1, "ocr_paginas": 3,
        "flash_hoy": 3, "max_dia": 6000,
        "motivo_parada": "completo", "elapsed_s": 7, "exit": 0,
    }, log_path=log)

    texto = log.read_text(encoding="utf-8")
    assert texto.startswith("ts=")
    assert "motivo_parada=completo" in texto
    assert "elapsed_s=7" in texto
    runs = supa.inserts("pipeline_runs")
    assert len(runs) == 1
    assert runs[0].insert_payload["paso"] == "ocr"
    assert runs[0].insert_payload["payload"]["motivo_parada"] == "completo"
    assert "motivo_parada=completo" in summary.read_text(encoding="utf-8")


def test_escribir_ocr_log_sin_summary_no_falla(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    log = tmp_path / "ultima_ocr.txt"
    selectivo.escribir_ocr_log(
        None, {"motivo_parada": "vacio"}, log_path=log)
    assert "motivo_parada=vacio" in log.read_text(encoding="utf-8")


def test_escribir_resumen_archivo_y_pipeline_runs(tmp_path):
    supa = FakeSupa()
    log = tmp_path / "ultima_pdf.txt"

    reportes.escribir_resumen(supa, {
        "modo": "todos", "ok": 3, "elapsed_s": 12,
    }, log_path=log)

    texto = log.read_text(encoding="utf-8")
    assert texto.startswith("ts=")
    assert "modo=todos" in texto
    runs = supa.inserts("pipeline_runs")
    assert len(runs) == 1
    assert runs[0].insert_payload["paso"] == "pdf"


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


# ── Fachada del entrypoint ─────────────────────────────────────────────────────

def test_entrypoint_reexporta_los_nombres_movidos():
    assert dr.registrar_meta_local is meta.registrar_meta_local
    assert dr.sync_meta_jsonl is meta.sync_meta_jsonl
    assert dr.columnas_extraccion_ok is meta.columnas_extraccion_ok
    assert dr.reporte_jsonl is meta.reporte_jsonl
    assert dr.META_LOG == meta.META_LOG
    assert dr.guardar_ok is persistencia.guardar_ok
    assert dr.guardar_ocr_progreso is persistencia.guardar_ocr_progreso
    assert dr.guardar_pendiente_ocr is persistencia.guardar_pendiente_ocr
    assert dr.guardar_sin_pdf is persistencia.guardar_sin_pdf
    assert dr.persistir_storage_si_hay is persistencia.persistir_storage_si_hay
    assert dr.conteo_pdf is reportes.conteo_pdf
    assert dr.reporte_extraccion is reportes.reporte_extraccion
    assert dr.group_by_tipo is reportes.group_by_tipo
    assert dr.escribir_resumen is reportes.escribir_resumen
    assert dr.cargar_cuota_ocr is ocr_cuota.cargar_cuota_ocr
    assert dr.guardar_cuota_ocr is ocr_cuota.guardar_cuota_ocr
    assert dr.registrar_ocr_ok is ocr_cuota.registrar_ocr_ok
    assert dr.CUOTA_OCR_TABLA == ocr_cuota.CUOTA_OCR_TABLA
    assert dr.CUOTA_OCR_PATH == ocr_cuota.CUOTA_OCR_PATH
    assert dr.contrato_ocr_sigue_elegible is (
        ocr_pendientes.contrato_ocr_sigue_elegible)
    assert dr.pendientes_ocr_paginas is ocr_pendientes.pendientes_ocr_paginas
    assert dr._as_int_list is ocr_pendientes._as_int_list
    assert dr.run_ocr_selectivo is selectivo.run_ocr_selectivo
    assert dr.escribir_ocr_log is selectivo.escribir_ocr_log
    assert dr.OCR_LOG == selectivo.OCR_LOG
    assert dr.anexar_ocr_a_tdr is pdf_extraction.anexar_ocr_a_tdr
    assert dr.payload_rechazo is ingestion_repository.payload_rechazo
    assert dr.payload_extraccion is meta.payload_extraccion
    assert dr.meta_local_por_id is meta.meta_local_por_id
    assert dr.REQ_PENDIENTE_OCR == "pendiente_ocr"
    assert dr.PAGE_DB == 1_000


# ── FIX-015: dead-letter de la cola OCR ───────────────────────────────────────

def test_selectivo_agotado_sale_de_cola_sin_gastar_ocr():
    captured: dict = {}
    fila = {"id": 7, "paginas_ocr_pendientes": [2, 3]}
    colas = iter([([fila], {}), ([], {})])
    ocr_calls: list = []
    agotados: list = []
    estados: list = []

    _run_selectivo(
        FakeSupa(), captured,
        pendientes=lambda *a, **k: next(colas),
        ocr_contrato=lambda *a, **k: ocr_calls.append(1),
        rechazos_ocr=lambda *a, **k: 9,
        agotar=lambda supa, c, rechazos=0: agotados.append(
            (int(c["id"]), rechazos)),
        imprimir=lambda i, n, cid, estado, detalle: estados.append(estado),
    )

    assert ocr_calls == []          # no gastó ni una página
    assert agotados == [(7, 9)]     # se vació con el conteo observado
    assert estados == ["OCR_AGOTADO"]
    assert captured["agotados"] == 1
    assert captured["err"] == 0


def test_selectivo_bajo_umbral_procesa_normal():
    captured: dict = {}
    fila = {"id": 8, "paginas_ocr_pendientes": [1]}
    colas = iter([([fila], {}), ([], {})])
    ocr_calls: list = []

    _run_selectivo(
        FakeSupa(), captured,
        pendientes=lambda *a, **k: next(colas),
        ocr_contrato=lambda *a, **k: ocr_calls.append(1) or {
            "nuevas": [1], "pend": []},
        rechazos_ocr=lambda *a, **k: 7,
    )

    assert ocr_calls == [1]         # 7 < umbral: sigue reintentando
    assert captured["agotados"] == 0


def test_rechazos_ocr_recientes_filtra_ventana_resueltos_y_sin_pdf():
    from seace_monitor.ocr.agotado import rechazos_ocr_recientes

    supa = FakeSupa(lambda q: SimpleNamespace(data=[], count=5))
    n = rechazos_ocr_recientes(supa, 42)

    assert n == 5
    q = supa.queries[0]
    assert q.table_name == "ingesta_rechazados"
    assert q.has_op("eq", "id_contrato", 42)
    assert q.has_op("eq", "origen", "pdf")
    assert q.has_op("eq", "resuelto", False)
    assert q.has_op("neq", "motivo", "sin archivo PDF")
    assert any(op[0] == "gte" and op[1][0] == "created_at" for op in q.ops)


def test_agotar_ocr_vacia_cola_audita_perdida_y_limpia_sidecar(tmp_path):
    from seace_monitor.ocr.agotado import agotar_ocr

    meta_file = tmp_path / "meta.jsonl"
    contrato = {
        "id": 55, "nro_contratacion": "X-1", "tdr_texto": "texto previo",
        "tdr_tipo_extraccion": "mixto", "tdr_n_paginas": 10,
        "paginas_ocr_pendientes": [4, 5], "paginas_ocr_hechas": [1, 2, 3],
    }
    supa = FakeSupa()
    agotar_ocr(supa, contrato, rechazos=9, meta_path=meta_file)

    updates = [q for q in supa.queries
               if q.table_name == "contratos" and q.update_payload]
    assert updates and updates[0].update_payload == {
        "paginas_ocr_pendientes": []}
    inserts = [q for q in supa.queries
               if q.table_name == "ingesta_rechazados" and q.insert_payload]
    assert inserts
    row = inserts[0].insert_payload
    assert row["motivo"] == "ocr_agotado"
    assert row["origen"] == "ocr"
    assert row["payload"]["paginas_perdidas"] == [4, 5]
    assert row["payload"]["rechazos_ventana"] == 9
    side = json.loads(meta_file.read_text(encoding="utf-8").strip())
    assert side["paginas_ocr_pendientes"] == []
    assert side["paginas_ocr_hechas"] == [1, 2, 3]


def test_selectivo_pdf_truncado_cierra_contrato_y_audita():
    """La redescarga dentro de la cola OCR también detecta el origen corrupto."""
    captured: dict = {}
    fila = {"id": 9, "paginas_ocr_pendientes": [4],
            "paginas_ocr_hechas": [1, 2]}
    colas = iter([([fila], {}), ([], {})])
    cerrados: list = []
    rechazos: list = []
    estados: list = []

    _run_selectivo(
        FakeSupa(), captured,
        pendientes=lambda *a, **k: next(colas),
        ocr_contrato=lambda *a, **k: (_ for _ in ()).throw(
            PdfTruncado("n=704349 sin startxref/%%EOF en la cola")),
        guardar_truncado=lambda supa, cid, c: cerrados.append(
            (cid, list(c["paginas_ocr_hechas"]))),
        rechazar=lambda supa, payload, motivo, origen=None:
            rechazos.append((payload, motivo, origen)),
        imprimir=lambda i, n, cid, estado, *d: estados.append(estado),
    )

    assert cerrados == [(9, [1, 2])]         # sale de la cola, conserva hechas
    assert rechazos and rechazos[0][1] == "pdf truncado en origen"
    assert rechazos[0][2] == "pdf"
    assert estados == ["PDF_TRUNCADO"]
    assert captured["err"] == 1
    assert captured["agotados"] == 0


def test_guardar_pdf_truncado_marca_terminal_y_conserva_hechas(tmp_path):
    contrato = {
        "id": 66, "_pdf_nombre": "PC.494.pdf", "_pdf_archivo_id": 399699,
        "paginas_ocr_pendientes": [3, 4], "paginas_ocr_hechas": [1, 2],
        "n_paginas": 4, "n_paginas_ocr": 2,
    }
    meta_file = tmp_path / "meta.jsonl"
    supa = FakeSupa()

    persistencia.guardar_pdf_truncado(supa, 66, contrato, meta_path=meta_file)

    updates = [q for q in supa.queries
               if q.table_name == "contratos" and q.update_payload]
    payload = updates[0].update_payload
    assert payload["pdf_descargado"] is True      # sale de pendientes_pdf
    assert payload["req_url"] == "pdf_truncado"   # marcador terminal auditable
    assert payload["paginas_ocr_pendientes"] == []  # sale de la cola OCR
    assert payload["paginas_ocr_hechas"] == [1, 2]  # trabajo previo se conserva
    assert payload["pdf_archivo_id"] == 399699
    assert payload["pdf_nombre"] == "PC.494.pdf"
    side = json.loads(meta_file.read_text(encoding="utf-8").strip())
    assert side["paginas_ocr_pendientes"] == []
    assert side["paginas_ocr_hechas"] == [1, 2]


def test_analizar_postulables_reconoce_version_con_identidad():
    """Una fila '1.qwen.<hash>' ya cuenta como análisis del pdf actual."""
    import scripts.analizar_postulables as ap

    def responder(q):
        if q.table_name == "v_contratos_estado":
            return SimpleNamespace(data=[{"id": 7, "es_postulable": True}])
        if q.table_name == "contratos":
            return SimpleNamespace(data=[{
                "id": 7, "nro_contratacion": "1", "entidad": "E",
                "pdf_hash": "h1", "tdr_texto": "x" * 300,
                "fecha_fin_cotizacion": None,
            }])
        if q.table_name == "analisis_contrato":
            return SimpleNamespace(data=[{
                "contrato_id": 7, "pdf_hash": "h1",
                "prompt_version": "1.qwen.a3815aa6",
            }])
        return SimpleNamespace(data=[])

    supa = FakeSupa(responder)
    filas = ap.cargar_postulables_rest(supa)

    assert len(filas) == 1
    assert filas[0]["ya_en_bd"] is True   # antes salía False → re-análisis fantasma
    q_analisis = [q for q in supa.queries
                  if q.table_name == "analisis_contrato"][0]
    assert q_analisis.has_op(
        "or_", "prompt_version.eq.1,prompt_version.like.1.*")
