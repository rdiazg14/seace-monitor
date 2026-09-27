"""Caracterización de refresh_estados.py (QA-001).

El módulo es un entrypoint con lógica residual identificada en REF-006: la
selección del universo postulable, la cadencia Vigente/En-Evaluación, el
gate terminal y el GC de chunks. Estas pruebas fijan el comportamiento real
antes de cualquier extracción o integración (IA-005).

Contratos verificados con dobles — ninguna prueba toca Playwright, SEACE ni
Supabase reales.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import refresh_estados as re_mod


# ── Dobles ────────────────────────────────────────────────────────────────────

class _Res:
    def __init__(self, data):
        self.data = data


class _Query:
    """Cadena Supabase: registra operaciones y sirve datos por proveedor."""

    def __init__(self, supa, tabla):
        self._supa = supa
        self._tabla = tabla
        self._ops = []
        self._range = None
        self._delete = False

    # Filtros encadenables (devuelven self).
    def select(self, cols):
        self._ops.append(("select", cols))
        return self

    def eq(self, col, val):
        self._ops.append(("eq", col, val))
        return self

    def in_(self, col, vals):
        self._ops.append(("in_", col, tuple(vals)))
        return self

    def or_(self, cond):
        self._ops.append(("or_", cond))
        return self

    def lt(self, col, val):
        self._ops.append(("lt", col, val))
        return self

    def order(self, col, desc=False, nullsfirst=None):
        self._ops.append(("order", col, desc, nullsfirst))
        return self

    def delete(self):
        self._delete = True
        return self

    @property
    def not_(self):
        return self

    def is_(self, col, val):
        self._ops.append(("is_", col, val))
        return self

    def range(self, a, b):
        self._range = (a, b)
        return self

    def execute(self):
        return _Res(self._supa._servir(self))


class FakeSupa:
    """Supabase falso: `proveedor(tabla, ops, rng, delete)` decide las filas."""

    def __init__(self, proveedor):
        self._proveedor = proveedor
        self.llamadas = []

    def table(self, nombre):
        q = _Query(self, nombre)
        return q

    def _servir(self, q: _Query):
        filas = self._proveedor(q._tabla, q._ops, q._range, q._delete)
        if q._range:
            a, b = q._range
            return filas[a : b + 1]
        return filas


def _filas_estado(estados: dict[int, str]):
    """Proveedor que responde `contratos` por estado con filas {id, estado}."""

    def proveedor(tabla, ops, rng, delete):
        if tabla != "contratos":
            return []
        eq_estado = next((o[2] for o in ops if o[0] == "eq" and o[1] == "estado"), None)
        return [
            {"id": i, "estado": est, "estado_verificado_at": f"2026-01-{i:02d}"}
            for i, est in estados.items()
            if est == eq_estado
        ]

    return proveedor


# ── es_terminal: gate de lista positiva ──────────────────────────────────────

class TestEsTerminal:
    def test_id_4_es_terminal(self):
        assert re_mod.es_terminal(4, "Culminado") is True

    def test_id_terminal_gana_aunque_nombre_sea_distinto(self):
        assert re_mod.es_terminal(4, "Otro nombre") is True

    def test_nombres_terminales_son_terminales_sin_importar_id(self):
        for nom in re_mod.NOMBRES_TERMINAL:
            assert re_mod.es_terminal(2, nom) is True, nom

    def test_vigente_y_en_evaluacion_no_son_terminales(self):
        assert re_mod.es_terminal(2, "Vigente") is False
        assert re_mod.es_terminal(3, "En Evaluación") is False

    def test_id_desconocido_nunca_es_terminal(self):
        # Lista positiva: un idEstadoContrato nuevo del SEACE no dispara GC.
        assert re_mod.es_terminal(99, "Estado Nuevo") is False
        assert re_mod.es_terminal(99) is False


# ── paginar ───────────────────────────────────────────────────────────────────

class TestPaginar:
    def test_pagina_hasta_agotar_y_respeta_limit(self, monkeypatch):
        ids = list(range(1, 2_501))
        supa = FakeSupa(lambda t, ops, rng, d: [
            {"id": i} for i in ids
        ])
        out = re_mod.paginar(supa, "id", "Vigente")
        assert [r["id"] for r in out] == ids

        out_limit = re_mod.paginar(supa, "id", "Vigente", limit=1_100)
        assert len(out_limit) == 1_100
        assert out_limit[-1]["id"] == 1_100

    def test_order_verificado_usa_asc_nullsfirst(self):
        vistos = []

        def prov(tabla, ops, rng, delete):
            vistos.extend(ops)
            return []

        supa = FakeSupa(prov)
        re_mod.paginar(supa, "id", "En Evaluación", order_verificado=True)
        assert ("order", "estado_verificado_at", False, True) in vistos


# ── seleccionar_lote: cadencia ────────────────────────────────────────────────

class TestSeleccionarLote:
    def test_todos_los_vigentes_mas_evaluacion_rotativa(self):
        estados = {i: "Vigente" for i in range(1, 6)}
        estados.update({i: "En Evaluación" for i in range(6, 20)})
        supa = FakeSupa(_filas_estado(estados))

        lote = re_mod.seleccionar_lote(supa, max_evaluacion=5, limit=0)
        vigentes = [r for r in lote if r["estado"] == "Vigente"]
        ev = [r for r in lote if r["estado"] == "En Evaluación"]
        assert len(vigentes) == 5
        assert len(ev) == 5  # tope de evaluación

    def test_limit_recorta_vigentes_primero(self):
        estados = {i: "Vigente" for i in range(1, 6)}
        estados.update({i: "En Evaluación" for i in range(6, 20)})
        supa = FakeSupa(_filas_estado(estados))

        lote = re_mod.seleccionar_lote(supa, max_evaluacion=10, limit=4)
        assert len(lote) == 4
        assert all(r["estado"] == "Vigente" for r in lote)

    def test_limit_mayor_que_vigentes_deja_cupo_a_evaluacion(self):
        estados = {i: "Vigente" for i in range(1, 3)}
        estados.update({i: "En Evaluación" for i in range(3, 20)})
        supa = FakeSupa(_filas_estado(estados))

        lote = re_mod.seleccionar_lote(supa, max_evaluacion=10, limit=5)
        assert sum(1 for r in lote if r["estado"] == "Vigente") == 2
        assert sum(1 for r in lote if r["estado"] == "En Evaluación") == 3

    def test_max_evaluacion_cero_solo_vigentes(self):
        estados = {i: "Vigente" for i in range(1, 4)}
        estados.update({i: "En Evaluación" for i in range(4, 10)})
        supa = FakeSupa(_filas_estado(estados))

        lote = re_mod.seleccionar_lote(supa, max_evaluacion=0, limit=0)
        assert all(r["estado"] == "Vigente" for r in lote)


# ── seleccionar_postulables: universo accionable ──────────────────────────────

class TestSeleccionarPostulables:
    def test_consulta_vista_con_or_postulable_o_por_abrir(self):
        vistos = []

        def prov(tabla, ops, rng, delete):
            vistos.append((tabla, list(ops)))
            if tabla == "v_contratos_estado":
                return [{"id": i} for i in range(1, 4)]
            if tabla == "contratos":
                return [{"id": i, "estado": "Vigente", "estado_verificado_at": "x"}
                        for i in range(1, 4)]
            return []

        supa = FakeSupa(prov)
        out = re_mod.seleccionar_postulables(supa)

        vista_ops = next(ops for t, ops in vistos if t == "v_contratos_estado")
        assert ("or_", "es_postulable.eq.true,es_por_abrir.eq.true") in vista_ops
        assert [r["id"] for r in out] == [1, 2, 3]

    def test_sin_postulables_no_toca_contratos(self):
        tablas = []

        def prov(tabla, ops, rng, delete):
            tablas.append(tabla)
            return []

        supa = FakeSupa(prov)
        assert re_mod.seleccionar_postulables(supa) == []
        assert "contratos" not in tablas

    def test_lotes_de_80_para_in_de_contratos(self):
        ids_vista = list(range(1, 201))
        in_sizes = []

        def prov(tabla, ops, rng, delete):
            if tabla == "v_contratos_estado":
                return [{"id": i} for i in ids_vista]
            if tabla == "contratos":
                in_op = next(o[2] for o in ops if o[0] == "in_")
                in_sizes.append(len(in_op))
                return [{"id": i, "estado": "Vigente"} for i in in_op]
            return []

        supa = FakeSupa(prov)
        out = re_mod.seleccionar_postulables(supa)
        assert len(out) == 200
        assert in_sizes == [80, 80, 40]


# ── fetch_estado: contrato con la API SEACE ───────────────────────────────────

class _Resp:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def _page_con(respuestas):
    it = iter(respuestas)
    llamadas = []

    def get(url, params=None, timeout=None):
        llamadas.append((url, params))
        return next(it)

    return SimpleNamespace(request=SimpleNamespace(get=get)), llamadas


class TestFetchEstado:
    def test_proyeccion_valida_devuelve_id_y_nombre(self):
        page, _ = _page_con([
            _Resp(200, {"uitContratoCompletoProjection": {
                "idEstadoContrato": 3, "nomEstadoContrato": " En Evaluación "}}),
        ])
        info = re_mod.fetch_estado(page, 123, retries=0)
        assert info == {"id_estado": 3, "nom_estado": "En Evaluación"}

    def test_nombre_vacio_cae_al_mapa_por_id(self):
        page, _ = _page_con([
            _Resp(200, {"uitContratoCompletoProjection": {
                "idEstadoContrato": 2, "nomEstadoContrato": "  "}}),
        ])
        assert re_mod.fetch_estado(page, 1, retries=0)["nom_estado"] == "Vigente"

    def test_proyeccion_vacia_no_reintenta(self):
        page, llamadas = _page_con([
            _Resp(200, {"uitContratoCompletoProjection": {}}),
            _Resp(200, {}),
        ])
        assert re_mod.fetch_estado(page, 1, retries=2) is None
        assert len(llamadas) == 1

    def test_sin_id_estado_no_reintenta(self):
        page, llamadas = _page_con([
            _Resp(200, {"uitContratoCompletoProjection": {"nomEstadoContrato": "X"}}),
        ])
        assert re_mod.fetch_estado(page, 1, retries=2) is None
        assert len(llamadas) == 1

    def test_http_no_200_reintenta_y_luego_none(self, monkeypatch):
        monkeypatch.setattr(re_mod.time, "sleep", lambda *_: None)
        page, llamadas = _page_con([_Resp(500), _Resp(500), _Resp(500)])
        assert re_mod.fetch_estado(page, 1, retries=2) is None
        assert len(llamadas) == 3

    def test_error_transitorio_recupera_en_reintento(self, monkeypatch):
        monkeypatch.setattr(re_mod.time, "sleep", lambda *_: None)
        page, _ = _page_con([
            _Resp(503),
            _Resp(200, {"uitContratoCompletoProjection": {
                "idEstadoContrato": 4, "nomEstadoContrato": "Culminado"}}),
        ])
        info = re_mod.fetch_estado(page, 1, retries=2)
        assert info["id_estado"] == 4

    def test_excepcion_reintenta(self, monkeypatch):
        monkeypatch.setattr(re_mod.time, "sleep", lambda *_: None)
        llamadas = []

        def get(url, params=None, timeout=None):
            llamadas.append(1)
            raise TimeoutError("t")

        page = SimpleNamespace(request=SimpleNamespace(get=get))
        assert re_mod.fetch_estado(page, 1, retries=1) is None
        assert len(llamadas) == 2


# ── borrar_chunks / GC ────────────────────────────────────────────────────────

class TestBorrarChunks:
    def test_delete_filtra_por_contrato_id(self):
        vistos = []

        def prov(tabla, ops, rng, delete):
            vistos.append((tabla, delete, list(ops)))
            return [{"id": 1}, {"id": 2}]

        supa = FakeSupa(prov)
        n = re_mod.borrar_chunks(supa, 777)
        assert n == 2
        tabla, delete, ops = vistos[0]
        assert tabla == "chunks_tdr" and delete is True
        assert ("eq", "contrato_id", 777) in ops


class TestGcCierres:
    def _supa_gc(self, ids_candidatos, fallo_en=None):
        borrados = []

        def prov(tabla, ops, rng, delete):
            if tabla == "contratos":
                return [{"id": i, "estado": "Culminado",
                         "estado_verificado_at": "2020-01-01"}
                        for i in ids_candidatos]
            if tabla == "chunks_tdr" and delete:
                cid = next(o[2] for o in ops if o[0] == "eq")
                if cid == fallo_en:
                    raise RuntimeError("fallo delete")
                borrados.append(cid)
                return [{"id": cid}]
            return []

        supa = FakeSupa(prov)
        return supa, borrados

    def test_dry_run_no_borra(self):
        supa, borrados = self._supa_gc([1, 2, 3])
        n = re_mod.gc_cierres_antiguos(supa, dry_run=True)
        assert n == 0 and borrados == []

    def test_borra_chunks_de_cada_candidato(self):
        supa, borrados = self._supa_gc([1, 2, 3])
        n = re_mod.gc_cierres_antiguos(supa, dry_run=False)
        assert borrados == [1, 2, 3]
        assert n == 3

    def test_fallo_en_un_contrato_no_detiene_al_resto(self):
        supa, borrados = self._supa_gc([1, 2, 3], fallo_en=2)
        n = re_mod.gc_cierres_antiguos(supa, dry_run=False)
        assert borrados == [1, 3]
        assert n == 2

    def test_filtro_gc_usa_nombres_terminales_y_corte(self):
        vistos = []

        def prov(tabla, ops, rng, delete):
            if tabla == "contratos":
                vistos.extend(ops)
            return []

        supa = FakeSupa(prov)
        re_mod.gc_cierres_antiguos(supa, dry_run=True)
        in_op = next(o for o in vistos if o[0] == "in_" and o[1] == "estado")
        assert set(in_op[2]) == set(re_mod.NOMBRES_TERMINAL)
        lt_op = next(o for o in vistos if o[0] == "lt")
        assert lt_op[1] == "estado_verificado_at"


# ── upsert_lote_pg ────────────────────────────────────────────────────────────

class TestUpsertLotePg:
    def test_upsert_solo_toca_estado_y_verificado(self, monkeypatch):
        ejecutado = {}

        class Cur:
            def executemany(self, sql, params):
                ejecutado["sql"] = sql
                ejecutado["params"] = params

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Conn:
            def cursor(self):
                return Cur()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(re_mod, "connect", lambda dsn: Conn())
        lote = [
            {"id": 1, "estado": "Culminado", "estado_verificado_at": "t1",
             "otro_campo": "NO-TOCAR"},
        ]
        re_mod.upsert_lote_pg("dsn", lote)
        assert "on conflict (id) do update" in ejecutado["sql"]
        assert "estado_verificado_at = excluded.estado_verificado_at" in ejecutado["sql"]
        assert "otro_campo" not in ejecutado["sql"]
        assert ejecutado["params"] == [(1, "Culminado", "t1")]
