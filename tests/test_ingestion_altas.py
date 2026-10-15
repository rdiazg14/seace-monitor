"""Altas por presencia y parada por páginas conocidas (listado por fecha)."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import ingesta_completa as ic
from seace_monitor.classification import repository as classification_repository
from seace_monitor.ingestion import repository
from seace_monitor.ingestion.service import (
    ids_de_lote,
    motivo_parada,
    seleccionar_altas,
)


def _r(cid) -> dict:
    return {"idContrato": cid}


# ── Reglas puras ────────────────────────────────────────────────────────────

def test_id_menor_que_el_maximo_publicado_despues_se_da_de_alta() -> None:
    # Caso real 13/10/2026: la página 1 trae primero 97725 (publicado 13/10
    # 00:20) y después 101498 (publicado 12/10 22:37, ya en la base).
    altas = seleccionar_altas([_r(97725), _r(101498)], {101498}, set())
    assert [a["idContrato"] for a in altas] == [97725]


def test_ids_repetidos_entre_paginas_entran_una_sola_vez() -> None:
    tomados: set[int] = set()
    p1 = seleccionar_altas([_r(10), _r(11)], set(), tomados)
    p2 = seleccionar_altas([_r(11), _r(12)], set(), tomados)
    assert [a["idContrato"] for a in p1 + p2] == [10, 11, 12]


def test_ids_invalidos_se_descartan_como_antes() -> None:
    lote = [_r(None), _r(0), _r("x"), "basura", _r(5)]
    assert [a["idContrato"] for a in seleccionar_altas(lote, set(), set())] == [5]
    assert ids_de_lote([_r(5), _r(5), _r(None), _r(7)]) == [5, 7]


def _recorrer(paginas, existentes, *, conocidas=2, tope=10):
    """Simula el bucle de ingesta_completa con las funciones puras."""
    tomados: set[int] = set()
    altas: list[int] = []
    sin_altas = 0
    for numero, ids in enumerate(paginas, start=1):
        nuevos = seleccionar_altas([_r(i) for i in ids], existentes, tomados)
        altas += [n["idContrato"] for n in nuevos]
        sin_altas = 0 if nuevos else sin_altas + 1
        motivo = motivo_parada(
            pagina=numero, total_paginas=len(paginas), lote_vacio=not ids,
            paginas_sin_altas=sin_altas, paginas_conocidas=conocidas,
            max_paginas=tope,
        )
        if motivo:
            return altas, motivo, numero
    raise AssertionError("no paró")


def test_recorre_hasta_dos_paginas_enteras_conocidas() -> None:
    # Orden por fecha: lo nuevo arriba, mezclado con ids conocidos y menores.
    paginas = [[300, 120, 299], [250, 110, 298], [100, 101, 102],
               [103, 104, 105], [90, 91, 92]]
    existentes = {120, 110, 100, 101, 102, 103, 104, 105}
    altas, motivo, leidas = _recorrer(paginas, existentes)
    assert altas == [300, 299, 250, 298]
    assert (motivo, leidas) == ("paginas_conocidas", 4)


def test_una_pagina_conocida_aislada_no_corta() -> None:
    paginas = [[1, 2], [3, 4], [5, 6], [7, 8], [9, 10], [11, 12]]
    existentes = {3, 4, 7, 8, 9, 10, 11, 12}
    altas, motivo, leidas = _recorrer(paginas, existentes)
    assert altas == [1, 2, 5, 6]
    assert (motivo, leidas) == ("paginas_conocidas", 5)


def test_tope_de_paginas() -> None:
    paginas = [[i] for i in range(1, 100)]
    altas, motivo, leidas = _recorrer(paginas, set(), tope=3)
    assert (motivo, leidas, altas) == ("tope_paginas", 3, [1, 2, 3])


def test_recuperacion_sin_corte_por_conocidas() -> None:
    altas, motivo, _ = _recorrer([[1], [2], [3], [4]], {1, 2},
                                 conocidas=0, tope=0)
    assert (altas, motivo) == ([3, 4], "ultima_pagina")


def test_modo_completo_solo_corta_al_final() -> None:
    base = dict(paginas_conocidas=0, max_paginas=0)
    assert motivo_parada(pagina=3, total_paginas=9, lote_vacio=False,
                         paginas_sin_altas=5, **base) is None
    assert motivo_parada(pagina=9, total_paginas=9, lote_vacio=False,
                         paginas_sin_altas=0, **base) == "ultima_pagina"
    assert motivo_parada(pagina=2, total_paginas=9, lote_vacio=True,
                         paginas_sin_altas=0, **base) == "pagina_vacia"


# ── Consulta de existencia ──────────────────────────────────────────────────

class _Query:
    def __init__(self, data):
        self.data = data
        self.filtro: list[int] = []

    def select(self, cols):
        assert cols == "id"
        return self

    def in_(self, col, ids):
        assert col == "id"
        self.filtro = list(ids)
        return self

    def execute(self):
        return SimpleNamespace(
            data=[{"id": i} for i in self.filtro if i in self.data])


class _Supa:
    def __init__(self, data):
        self.query = _Query(data)

    def table(self, name):
        assert name == "contratos"
        return self.query


def test_existencia_por_postgrest() -> None:
    supa = _Supa({101498})
    assert repository.ids_existentes(supa, [97725, 101498]) == {101498}
    assert repository.ids_existentes(supa, []) == set()


def test_existencia_por_conexion_directa(monkeypatch) -> None:
    llamadas = []

    class _Cur:
        def fetchall(self):
            return [(101498,)]

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params):
            llamadas.append((sql, params))
            return _Cur()

    monkeypatch.setattr(repository, "connect", lambda dsn: _Conn())
    assert repository.ids_existentes(None, [97725, 101498], dsn="x") == {101498}
    assert "any(%s)" in llamadas[0][0]
    assert llamadas[0][1] == ([97725, 101498],)


def test_sin_base_ni_cliente_no_supone_que_nada_existe() -> None:
    with pytest.raises(RuntimeError):
        repository.ids_existentes(None, [1])


# ── Bucle real de ingesta_completa.main, sin red ni base ────────────────────

class _Page:
    def goto(self, *args, **kwargs):
        return None

    def wait_for_timeout(self, *args):
        return None


class _Browser:
    def new_context(self, **kwargs):
        return SimpleNamespace(new_page=lambda: _Page())

    def close(self):
        return None


class _Playwright:
    chromium = SimpleNamespace(launch=lambda **kwargs: _Browser())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _registro(cid: int) -> dict:
    return {
        "idContrato": cid,
        "nroContratacion": f"CM-{cid}-2026",
        "desContratacion": f"Servicio {cid}",
        "nomObjetoContrato": "Servicio",
        "desObjetoContrato": f"Servicio {cid}",
        "nomEntidad": "ENTIDAD DE PRUEBA",
        "nomEstadoContrato": "Vigente",
        "fecPublica": "13/10/2026 00:20:00",
        "idTipoCotizacion": 1,
        "cotizar": True,
    }


def _ejecutar_main(monkeypatch, tmp_path, paginas, *, max_id, existentes,
                   argv=(), falla_existencia=False):
    """Corre main() con el listado ``paginas`` y todo lo externo reemplazado."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://prueba-sin-conexion")
    monkeypatch.setattr(sys, "argv", ["ingesta_completa.py", *argv])
    supa = object()
    leidas: list[int] = []
    consultas: list[list[int]] = []
    escritas: list[int] = []
    runs: list[dict] = []

    def api_call(page, numero):
        leidas.append(numero)
        lote = [_registro(i) for i in paginas[numero - 1]]
        return len(paginas) * 100, lote, 200

    def ids_existentes(client, ids, *, dsn=""):
        assert client is supa and dsn
        if falla_existencia:
            raise ConnectionError("sin red")
        consultas.append(list(ids))
        return {i for i in ids if i in existentes}

    monkeypatch.setattr(ic, "init_supabase", lambda: supa)
    monkeypatch.setattr(ic, "cargar_keywords", lambda client: None)
    monkeypatch.setattr(ic, "get_max_id_supabase", lambda client: max_id)
    monkeypatch.setattr(ic, "sync_playwright", lambda: _Playwright())
    monkeypatch.setattr(ic, "_api_call", api_call)
    monkeypatch.setattr(ic, "ids_existentes", ids_existentes)
    monkeypatch.setattr(ic, "filtrar_validos", lambda raw, client: (raw, 0))
    monkeypatch.setattr(
        ic, "upsert_contratos_pg",
        lambda dsn, filas: escritas.extend(int(f["id"]) for f in filas))
    monkeypatch.setattr(ic, "upsert_supabase", lambda client, filas: 0)
    monkeypatch.setattr(
        ic, "escribir_clasificacion_keyword", lambda filas, supa=None: (0, 0))
    monkeypatch.setattr(
        classification_repository, "anunciar_backend_capa3",
        lambda supa=None: None)
    monkeypatch.setattr(ic, "_guardar_csv", lambda df: None)
    monkeypatch.setattr(
        ic, "registrar_run", lambda client, paso, payload: runs.append(payload))

    ic.main()
    return SimpleNamespace(
        leidas=leidas, consultas=consultas, escritas=escritas, run=runs[-1])


def test_main_da_de_alta_el_caso_real_y_registra_la_parada(
    monkeypatch, tmp_path,
) -> None:
    paginas = [[97725, 101498], [101400, 101300], [101200, 101100], [1, 2]]
    r = _ejecutar_main(
        monkeypatch, tmp_path, paginas, max_id=101498,
        existentes={101498, 101400, 101300, 101200, 101100})

    assert r.escritas == [97725]
    assert r.leidas == [1, 2, 3]
    assert r.consultas[0] == [97725, 101498]
    assert r.run["nuevos_esta_corrida"] == 1
    assert r.run["paginas_leidas"] == 3
    assert r.run["motivo_parada"] == "paginas_conocidas"
    assert r.run["alerta_anomala"] == 0


def test_main_tope_por_defecto_es_diez_paginas(monkeypatch, tmp_path) -> None:
    paginas = [[1000 + i] for i in range(30)]
    r = _ejecutar_main(monkeypatch, tmp_path, paginas, max_id=5000,
                       existentes=set())

    assert r.leidas == list(range(1, 11))
    assert len(r.escritas) == 10
    assert r.run["motivo_parada"] == "tope_paginas"
    assert r.run["paginas_leidas"] == 10


def test_main_sin_consulta_de_existencia_se_detiene_con_alerta(
    monkeypatch, tmp_path,
) -> None:
    paginas = [[7, 8], [5, 6], [3, 4]]
    r = _ejecutar_main(monkeypatch, tmp_path, paginas, max_id=100,
                       existentes=set(), falla_existencia=True)

    # No supone que nada existe: no escribe ni sigue paginando, pero termina
    # sin error para no tumbar la cadena diaria.
    assert r.leidas == [1]
    assert r.escritas == []
    assert r.run["alerta_anomala"] == 1
    assert r.run["motivo_parada"] == "error_existencia"


def test_main_modo_completo_sin_cambios(monkeypatch, tmp_path) -> None:
    paginas = [[3, 2], [2, 1], [1, 9]]
    r = _ejecutar_main(monkeypatch, tmp_path, paginas, max_id=0,
                       existentes={1, 2, 3})

    # Base vacía: recorre todo el listado, no consulta existencia ni deduplica.
    assert r.leidas == [1, 2, 3]
    assert r.consultas == []
    assert r.run["nuevos_esta_corrida"] == 6
    assert r.run["motivo_parada"] == "ultima_pagina"
