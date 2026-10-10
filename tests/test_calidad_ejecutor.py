"""Ejecutor del dataset de calidad (GW-006): presupuesto, captura y registro, sin red."""

import json
from datetime import date

import pytest

from seace_monitor.calidad.ejecutor import correr, ejecutar, guardar, observar
from seace_monitor.calidad.evaluacion import Caso

APROBADA = {"estado": "aprobado", "revisor": "revisora", "fecha": "2026-10-10"}


def caso(i: int, **over) -> Caso:
    base = {
        "id": f"c{i}", "tipo": "recuperacion", "pregunta": f"servicio {i}",
        "contratos_relevantes": [i], "fuentes": [{"contrato_id": i, "pdf_hash": f"h{i}"}], "revision": APROBADA,
    }
    return Caso.model_validate({**base, **over})


def proxy(costo=0.0002, ids=None, **extra):
    llamadas = []

    def consultar(pregunta):
        llamadas.append(pregunta)
        n = int(pregunta.split()[-1])
        cuerpo = {"respuesta": f"respuesta {n}", "contratos_referenciados": [{"id": i} for i in (ids or [n])],
                  "costo_usd": costo, "served_by": "qwen3.7-flash", **extra}
        return 200, cuerpo

    return consultar, llamadas


def test_captura_contratos_respuesta_costo_y_modelo():
    consultar, _ = proxy(ids=[7, 3])
    obs, costo, modelo = observar(caso(3), consultar)
    assert (obs.http_status, obs.contratos_devueltos, obs.respuesta, obs.error) == (200, [7, 3], "respuesta 3", None)
    assert (costo, modelo) == (0.0002, "qwen3.7-flash")


def test_error_del_proveedor_con_http_200_no_pasa_por_acierto():
    consultar, _ = proxy(error="gemini generate HTTP 429: quota")
    obs, _, _ = observar(caso(1), consultar)
    assert obs.error.startswith("gemini generate HTTP 429")


def test_fallo_de_transporte_es_observacion_de_error_no_excepcion():
    def consultar(_):
        raise TimeoutError("sin respuesta")

    obs, costo, modelo = observar(caso(1), consultar)
    assert (obs.error, obs.http_status, costo, modelo) == ("TimeoutError", None, 0.0, None)


def test_se_detiene_antes_de_superar_el_presupuesto():
    consultar, llamadas = proxy(costo=0.004)
    casos = [caso(i) for i in range(1, 8)]
    obs, gastado, _ = ejecutar(casos, consultar, presupuesto_usd=0.02, costo_maximo_por_caso_usd=0.01)
    # Reserva 0,01 por caso: tras gastar 0,012 ya no cabe otra reserva.
    assert len(llamadas) == 3
    assert gastado == pytest.approx(0.012)
    assert gastado <= 0.02
    assert [o.caso_id for o in obs] == ["c1", "c2", "c3"]


def test_solo_ejecuta_casos_aprobados_y_exige_presupuesto_positivo():
    consultar, llamadas = proxy()
    casos = [caso(1), caso(2, revision={"estado": "pendiente"}), caso(3, revision={"estado": "rechazado"})]
    ejecutar(casos, consultar, presupuesto_usd=1.0)
    assert llamadas == ["servicio 1"]
    with pytest.raises(ValueError):
        ejecutar(casos, consultar, presupuesto_usd=0)


def test_corrida_completa_deja_registro_comparable(tmp_path):
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps({"casos": [caso(1).model_dump(mode="json"), caso(2).model_dump(mode="json")]}), encoding="utf-8")
    consultar, _ = proxy()
    reg = correr(dataset, consultar, presupuesto_usd=1.0, umbral=0.9, corpus_espacio="qwen-tev4-1536",
                 prompt_version="1.qwen.a3815aa6", config_version=47, hoy=date(2026, 10, 10))
    assert reg["resumen"]["casos"] == 2 and reg["resumen"]["ok"] == 2 and reg["resumen"]["cumple"] is True
    assert reg["corrida"]["modelo"] == "qwen3.7-flash"
    assert (reg["gasto_usd"], reg["casos_aprobados"], reg["casos_ejecutados"], reg["detenida_por_presupuesto"]) == (0.0004, 2, 2, False)
    destino = tmp_path / "sub" / "corrida.json"
    guardar(reg, destino)
    assert json.loads(destino.read_text(encoding="utf-8"))["dataset_sha256"] == reg["dataset_sha256"]


def test_corrida_cortada_por_presupuesto_no_declara_cumplimiento_falso(tmp_path):
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps({"casos": [caso(i).model_dump(mode="json") for i in range(1, 6)]}), encoding="utf-8")
    consultar, _ = proxy(costo=0.009)
    reg = correr(dataset, consultar, presupuesto_usd=0.02, umbral=0.9, corpus_espacio="e", prompt_version="p",
                 config_version=None, hoy=date(2026, 10, 10))
    assert reg["detenida_por_presupuesto"] is True
    assert reg["resumen"]["sin_observacion"] == 3
    assert reg["resumen"]["cumple"] is False


def test_failover_a_mitad_de_corrida_se_declara_en_el_modelo(tmp_path):
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps({"casos": [caso(1).model_dump(mode="json"), caso(2).model_dump(mode="json")]}), encoding="utf-8")
    modelos = iter(["qwen3.7-flash", "gemini-3.7-flash"])

    def consultar(pregunta):
        n = int(pregunta.split()[-1])
        return 200, {"respuesta": "r", "contratos_referenciados": [{"id": n}], "costo_usd": 0.0001, "served_by": next(modelos)}

    reg = correr(dataset, consultar, presupuesto_usd=1.0, umbral=0.9, corpus_espacio="e", prompt_version="p",
                 config_version=None, hoy=date(2026, 10, 10))
    assert reg["corrida"]["modelo"] == "mixto:gemini-3.7-flash,qwen3.7-flash"
