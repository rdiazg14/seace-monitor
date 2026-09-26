"""REF-006: caracterización del pipeline C1/C4 extraído a classification/.

Cubre cuota C4, ledger de rechazadas, cola de revisión, cruce de consenso,
--aplicar con re-select y el camino directo — con Supabase y Gemini falsos.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from seace_monitor.classification.artefactos import conteos_items
from seace_monitor.classification.cola import persistir_cola_revision, upsert_cola_tabla
from seace_monitor.classification.contracts import SYSTEM_PROMPT_P1, SYSTEM_PROMPT_P2
from seace_monitor.classification.cuota import (
    CupoClasificacion,
    acumular_tokens,
    assert_cuota_c4,
    cargar_cuota_c4,
    guardar_cuota_c4,
    registrar_llamada_c4,
    stats_tokens,
)
from seace_monitor.classification.ledger import (
    aplicar_ledger,
    cargar_ledger,
    categoria_efectiva,
    categoria_propuesta_escritura,
)
from seace_monitor.classification.workflow import (
    ConfigClasificacion,
    camino_directo,
    comando_aplicar,
    comando_consenso,
    comando_proponer,
)
from seace_monitor.gemini import fecha_lima


# ---------- dobles ----------


class FakeQuery:
    def __init__(self, client, tabla):
        self.client = client
        self.tabla = tabla
        self.payload = None
        self.single = False

    def select(self, *a, **kw):
        return self

    def eq(self, *a, **kw):
        return self

    def in_(self, *a, **kw):
        return self

    def is_(self, *a, **kw):
        return self

    def order(self, *a, **kw):
        return self

    def gte(self, *a, **kw):
        return self

    def limit(self, *a, **kw):
        return self

    def range(self, *a, **kw):
        return self

    def maybe_single(self):
        self.single = True
        return self

    def upsert(self, payload, on_conflict=None):
        self.payload = payload
        return self

    def insert(self, payload):
        self.payload = payload
        return self

    def execute(self):
        if self.payload is not None:
            rows = self.payload if isinstance(self.payload, list) else [self.payload]
            self.client.upserts.setdefault(self.tabla, []).extend(rows)
            self.payload = None
            return SimpleNamespace(data=[])
        data = list(self.client.tables.get(self.tabla, []))
        if getattr(self, "single", False):
            return SimpleNamespace(data=data[0] if data else None)
        return SimpleNamespace(data=data)


class FakeSupabase:
    def __init__(self, **tables):
        self.tables = dict(tables)
        self.upserts: dict[str, list[dict]] = {}

    def table(self, name):
        return FakeQuery(self, name)


def cfg_fake(tmp_path: Path, **kw) -> ConfigClasificacion:
    defaults = dict(
        api_key="k",
        url="https://gemini.test/x",
        modelo="gemini-test",
        supa=None,
        supa_opcional=lambda: None,
        conectar_supa=lambda: None,
        data_dir=tmp_path,
        cuota_path=tmp_path / "cuota.json",
        ledger_path=tmp_path / "ledger.json",
        cola_path=tmp_path / "cola.json",
    )
    defaults.update(kw)
    return ConfigClasificacion(**defaults)


def artefacto(tmp_path: Path, nombre: str, items: list[dict], **meta) -> Path:
    p = tmp_path / nombre
    base = {
        "generado_utc": datetime.now(timezone.utc).isoformat(),
        "filtro": "vigentes",
        "version_c": "C1.5",
        "incluir_ventana_cerrada": False,
    }
    base.update(meta)
    p.write_text(json.dumps({"meta": base, "items": items}), encoding="utf-8")
    return p


def item_escribir(cid: int, cat: str = "Hardware") -> dict:
    return {
        "id": cid,
        "decision": "escribir",
        "origen": "alta_directa",
        "p1": {"categoria": cat, "confianza": "alta", "senal": "x"},
        "p2": None,
    }


# ---------- cuota C4 ----------


def test_cuota_c4_local_fallback_tope_y_registro(tmp_path):
    path = tmp_path / "cuota.json"
    assert cargar_cuota_c4(None, hoy="2026-09-26", path=path) == {
        "fecha": "2026-09-26",
        "requests": 0,
        "prompt_tokens": 0,
        "candidates_tokens": 0,
        "total_tokens": 0,
    }

    registrar_llamada_c4(
        None,
        {"usageMetadata": {
            "promptTokenCount": 10,
            "candidatesTokenCount": 5,
            "totalTokenCount": 15,
        }},
        max_llamadas=10,
        path=path,
    )
    assert path.exists()
    d = json.loads(path.read_text(encoding="utf-8"))
    assert d["requests"] == 1
    assert d["prompt_tokens"] == 10 and d["total_tokens"] == 15

    guardar_cuota_c4(
        None,
        {"fecha": fecha_lima(), "requests": 10, "prompt_tokens": 0,
         "candidates_tokens": 0, "total_tokens": 0},
        path=path,
    )
    with pytest.raises(CupoClasificacion):
        assert_cuota_c4(None, max_llamadas=10, path=path)
    assert_cuota_c4(None, max_llamadas=11, path=path)


def test_cuota_c4_prefiere_bd_sobre_archivo(tmp_path):
    path = tmp_path / "cuota.json"
    supa = FakeSupabase(pipeline_cuota_c4=[{
        "fecha_lima": "hoy",
        "requests": 7,
        "prompt_tokens": 70,
        "candidates_tokens": 30,
        "total_tokens": 100,
    }])

    d = cargar_cuota_c4(supa, hoy="hoy", path=path)

    assert d["requests"] == 7 and d["total_tokens"] == 100


def test_acumular_tokens_conteo():
    stats = stats_tokens()
    acumular_tokens(stats, {"usageMetadata": {
        "promptTokenCount": 4, "candidatesTokenCount": 2,
        "totalTokenCount": 6,
    }})
    acumular_tokens(stats, {})
    assert stats == {"prompt": 4, "candidates": 2, "total": 6, "llamadas": 2}


# ---------- ledger ----------


def test_ledger_fusiona_json_y_rechazadas_de_tabla(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps([
        {"id": 1, "categoria_rechazada": "Hardware", "fecha": "2026-01-01"},
    ]))
    supa = FakeSupabase(clasificacion_pendiente=[{
        "contrato_id": 2,
        "categoria_p1": "Licencias",
        "categoria_p2": "ninguna",
        "votos": {"a.json": "Redes", "b.json": "ninguna"},
        "estado": "rechazada",
    }])

    ledger = cargar_ledger(supa, path=path)

    pares = {(e["id"], e["categoria_rechazada"]) for e in ledger}
    assert pares == {(1, "Hardware"), (2, "Licencias"), (2, "Redes")}


def test_aplicar_ledger_baja_propuesta_repetida():
    items = [
        item_escribir(1, "Hardware"),
        item_escribir(2, "Licencias"),
    ]
    ledger = [{"id": 1, "categoria_rechazada": "Hardware", "fecha": "f"}]

    aplicar_ledger(items, ledger)

    assert items[0]["decision"] == "rechazado_previo"
    assert items[0]["en_ledger"] is True
    assert items[1]["decision"] == "escribir"
    assert items[1]["en_ledger"] is False


def test_categoria_efectiva_desempate_usa_p2():
    it = item_escribir(1)
    it["origen"] = "desempate_ok"
    it["p2"] = {"categoria": "Licencias"}
    assert categoria_propuesta_escritura(it) == "Licencias"
    assert categoria_efectiva(it) == "Licencias"

    it["decision"] = "cola"
    assert categoria_efectiva(it) == "ninguna"


# ---------- cola de revisión ----------


def test_cola_preserva_no_pendientes_y_agrega(tmp_path):
    cola = tmp_path / "cola.json"
    cola.write_text(json.dumps({"items": [
        {"id": 1, "estado": "aprobada", "p1": "Hardware"},
        {"id": 9, "estado": "pendiente", "p1": "Vieja"},
    ]}))
    items = [
        {**item_escribir(1, "Licencias"), "revisar": True},
        {**item_escribir(2, "Redes"), "revisar": True, "descripcion": "sw"},
    ]
    ahora = datetime.now(timezone.utc)

    persistir_cola_revision(
        items, tmp_path / "art.json", ahora, path=cola, supa=None
    )

    out = {e["id"]: e for e in json.loads(cola.read_text())["items"]}
    assert out[1]["estado"] == "aprobada"          # no pisada
    assert out[2]["estado"] == "pendiente"          # agregada
    assert out[9]["estado"] == "pendiente"          # conservada


def test_upsert_cola_tabla_no_pisa_resueltas():
    supa = FakeSupabase(clasificacion_pendiente=[
        {"contrato_id": 1, "estado": "aprobada"},
        {"contrato_id": 2, "estado": "pendiente"},
    ])
    items = [
        {**item_escribir(1), "revisar": True},
        {**item_escribir(2), "revisar": True, "p2": {"categoria": "Redes"}},
        item_escribir(3),  # sin revisar ni cola -> no sube
    ]
    ahora = datetime.now(timezone.utc)

    n = upsert_cola_tabla(supa, items, Path("art.json"), ahora)

    assert n == 1
    escritas = supa.upserts["clasificacion_pendiente"]
    assert [r["contrato_id"] for r in escritas] == [2]
    assert escritas[0]["categoria_p2"] == "Redes"
    assert escritas[0]["estado"] == "pendiente"


# ---------- consenso ----------


def test_consenso_aborta_con_metadatos_distintos(tmp_path):
    cfg = cfg_fake(tmp_path)
    a = artefacto(tmp_path, "a.json", [], filtro="vigentes")
    b = artefacto(tmp_path, "b.json", [], filtro="todos")
    assert comando_consenso(cfg, [str(a), str(b)]) == 6

    b2 = artefacto(tmp_path, "b2.json", [], filtro="vigentes")
    payload = json.loads(b2.read_text())
    payload["aplicado"] = {"utc": "x"}
    b2.write_text(json.dumps(payload))
    assert comando_consenso(cfg, [str(a), str(b2)]) == 6


def test_consenso_cruza_votos_y_respeta_orden(tmp_path):
    cfg = cfg_fake(tmp_path)
    items_a = [
        item_escribir(1, "Hardware"),
        item_escribir(2, "Licencias"),
        {**item_escribir(3), "decision": "cola", "origen": "discrepa_es_it"},
    ]
    items_b = [
        item_escribir(2, "Licencias"),
        item_escribir(1, "Hardware"),
        {**item_escribir(3), "decision": "cola", "origen": "discrepa_es_it"},
    ]
    items_c = [
        item_escribir(1, "Hardware"),
        {**item_escribir(2), "decision": "no_escribir", "origen": "ninguna",
         "p1": {"categoria": "ninguna"}},
        {**item_escribir(3), "decision": "escribir", "origen": "alta_directa"},
    ]
    a = artefacto(tmp_path, "a.json", items_a)
    b = artefacto(tmp_path, "b.json", items_b)
    c = artefacto(tmp_path, "c.json", items_c)

    assert comando_consenso(cfg, [str(a), str(b), str(c)]) == 0

    out = [p for p in tmp_path.glob("consenso_it_*.json")]
    assert len(out) == 1
    payload = json.loads(out[0].read_text())
    assert payload["meta"]["n_corridas"] == 3
    por_id = {it["id"]: it for it in payload["items"]}
    assert por_id[1]["decision"] == "escribir"
    assert por_id[1]["origen"] == "consenso_unanime"
    assert por_id[1]["votos"] == {
        "a.json": "Hardware", "b.json": "Hardware", "c.json": "Hardware",
    }
    assert por_id[2]["origen"] == "consenso_inestable"
    assert por_id[2]["revisar"] is True
    assert por_id[3]["origen"] == "consenso_inestable"
    assert payload["meta"]["conteos"] == {
        "consenso_unanime": 1,
        "consenso_ninguna": 0,
        "consenso_inestable": 2,
    }


# ---------- aplicar ----------


def test_aplicar_rechaza_inexistente_reaplicado_y_viejo(tmp_path):
    cfg = cfg_fake(tmp_path)
    assert comando_aplicar(cfg, str(tmp_path / "no.json")) == 1

    a = artefacto(tmp_path, "aplicado.json", [item_escribir(1)])
    payload = json.loads(a.read_text())
    payload["aplicado"] = {"utc": "x"}
    a.write_text(json.dumps(payload))
    assert comando_aplicar(cfg, str(a)) == 5

    viejo = artefacto(
        tmp_path, "viejo.json", [item_escribir(1)],
        generado_utc=(datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),
    )
    assert comando_aplicar(cfg, str(viejo)) == 4


def test_aplicar_escribe_solo_sin_fila_y_marca_aplicado(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    supa = FakeSupabase(clasificacion_contrato=[
        {"contrato_id": 1, "categoria_it": "Hardware",
         "relevancia_ia": None, "capa": "keyword"},
    ])
    registradas: list[list[dict]] = []
    cfg = cfg_fake(
        tmp_path,
        conectar_supa=lambda: supa,
        registrar_keywords=lambda s, items: (registradas.append(items), (0, 0))[1],
    )
    items = [
        item_escribir(1),
        {**item_escribir(2, "Licencias"), "origen": "desempate_ok",
         "p2": {"categoria": "Licencias", "senal": "office",
                "senal_fuente": "descripcion", "confianza": "alta"}},
        {"id": 3, "decision": "cola"},
    ]
    a = artefacto(tmp_path, "con.json", items, n_corridas=3)

    assert comando_aplicar(cfg, str(a)) == 0

    escritos = supa.upserts["clasificacion_contrato"]
    assert [r["contrato_id"] for r in escritos] == [2]
    assert escritos[0]["capa"] == "gemini"
    assert escritos[0]["consenso_n"] == 3
    assert escritos[0]["artefacto"] == "con.json"
    assert escritos[0]["senal"] == "office"

    payload = json.loads(a.read_text())
    assert payload["aplicado"]["escritos"] == [2]
    assert payload["aplicado"]["descartados"] == [1]
    assert registradas == [items[:2]]


# ---------- proponer (pasadas P1/P2) ----------


def fila(cid: int, desc: str) -> dict:
    return {
        "id": cid,
        "descripcion": desc,
        "entidad": "Entidad",
        "objeto": "Bien",
        "items_json": [],
    }


def clasificar_fake(respuestas: dict[str, dict[int, dict]]):
    def _fn(client, lote, *, system_prompt, schema, armar_prompt, **kw):
        pasada = respuestas.get(system_prompt, {})
        return [dict(pasada[int(r["id"])], id=int(r["id"])) for r in lote
                if int(r["id"]) in pasada]
    return _fn


def test_proponer_desempate_ok_ninguna_y_discrepancia(tmp_path):
    filas = [
        fila(1, "Adquisicion de laptop Core i7"),
        fila(2, "Servicio de limpieza"),
        fila(3, "Adquisicion de monitor 24 pulgadas"),
    ]
    respuestas = {
        SYSTEM_PROMPT_P1: {
            1: {"categoria": "Hardware", "confianza": "alta",
                "senal": "laptop"},
            2: {"categoria": "ninguna", "senal": ""},
            3: {"categoria": "Hardware", "confianza": "alta",
                "senal": "monitor"},
        },
        SYSTEM_PROMPT_P2: {
            1: {"categoria": "Hardware", "senal": "laptop"},
            3: {"categoria": "Licencias", "senal": "monitor"},
        },
    }
    cfg = cfg_fake(tmp_path, clasificar=clasificar_fake(respuestas))

    assert comando_proponer(
        cfg, filas=filas, filtro="vigentes", limit=0, batch=10,
        incluir_ventana_cerrada=False,
    ) == 0

    out = list(tmp_path.glob("propuestas_it_*.json"))
    assert len(out) == 1
    payload = json.loads(out[0].read_text())
    por_id = {it["id"]: it for it in payload["items"]}
    assert por_id[1]["decision"] == "escribir"
    assert por_id[1]["origen"] == "desempate_ok"
    assert por_id[2]["decision"] == "no_escribir"
    assert por_id[2]["origen"] == "ninguna"
    # discrepa_intra_it: escribe P1 pero queda marcada para revisión
    assert por_id[3]["decision"] == "escribir"
    assert por_id[3]["origen"] == "discrepa_intra_it"
    assert por_id[3]["revisar"] is True
    assert categoria_efectiva(por_id[3]) == "Hardware"
    assert payload["meta"]["conteos"]["desempate_ok"] == 1
    assert payload["meta"]["conteos"]["ninguna"] == 1
    assert payload["meta"]["conteos"]["discrepa_intra_it"] == 1
    # la discrepante entra a la cola de revisión
    cola = json.loads((tmp_path / "cola.json").read_text())
    assert [e["id"] for e in cola["items"]] == [3]


def test_proponer_lote_fallido_marca_sin_respuesta_exit_3(tmp_path):
    def falla(client, lote, **kw):
        raise RuntimeError("gemini caido")

    cfg = cfg_fake(tmp_path, clasificar=falla)
    assert comando_proponer(
        cfg, filas=[fila(1, "x")], filtro="vigentes", limit=0, batch=10,
        incluir_ventana_cerrada=False,
    ) == 3
    payload = json.loads(
        list(tmp_path.glob("propuestas_it_*.json"))[0].read_text()
    )
    assert payload["items"][0]["decision"] == "sin_respuesta"
    assert payload["meta"]["conteos"]["sin_respuesta"] == 1


# ---------- camino directo ----------


def test_camino_directo_dry_run_no_escribe(tmp_path):
    llamadas: list[int] = []

    def fake(client, lote, **kw):
        llamadas.append(len(lote))
        return [{"id": 1, "categoria": "Hardware"},
                {"id": 2, "categoria": "ninguna"}]

    cfg = cfg_fake(tmp_path, clasificar=fake)
    assert camino_directo(
        cfg, filas=[fila(1, "laptop"), fila(2, "limpieza")],
        batch=10, dry_run=True,
    ) == 0
    assert llamadas == [2]


def test_camino_directo_escribe_solo_it(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    supa = FakeSupabase(clasificacion_contrato=[])

    def fake(client, lote, **kw):
        return [{"id": 1, "categoria": "Hardware"},
                {"id": 2, "categoria": "ninguna"}]

    cfg = cfg_fake(tmp_path, supa=supa, clasificar=fake)
    assert camino_directo(
        cfg, filas=[fila(1, "laptop"), fila(2, "limpieza")],
        batch=10, dry_run=False,
    ) == 0
    escritos = supa.upserts["clasificacion_contrato"]
    assert [r["contrato_id"] for r in escritos] == [1]
    assert escritos[0]["artefacto"] == "camino_directo"
    assert escritos[0]["consenso_n"] == 0


# ---------- conteos ----------


def test_conteos_items_claves_estables():
    counts = conteos_items([
        {"decision": "escribir", "origen": "alta_directa",
         "p1": {"categoria": "Hardware", "senal_fuente": "item"}},
        {"decision": "cola", "origen": "discrepa_es_it", "revisar": True,
         "p2": {"senal_verificada": False}},
    ])
    assert counts["alta_directa"] == 1
    assert counts["discrepa_es_it"] == 1
    assert counts["revisar"] == 1
    assert counts["senal_fuente_item"] == 1
    assert counts["p2_senal_no_verificada"] == 1
