"""Dataset de calidad (GW-006): validación de casos y puntuación local, sin red.

Los casos de estas pruebas son sintéticos; no representan TDR reales.
"""

import json
from datetime import date

import pytest
from pydantic import ValidationError

from seace_monitor.calidad.evaluacion import (
    Caso,
    Corrida,
    Observacion,
    cargar_dataset,
    evaluar,
    huella_dataset,
    puntuar,
    registro_corrida,
    resumen,
)

APROBADA = {"estado": "aprobado", "revisor": "revisora", "fecha": "2026-10-10"}
FUENTE = [{"contrato_id": 11, "pdf_hash": "abc"}]


def caso(**over) -> Caso:
    base = {
        "id": "c1", "tipo": "respuesta", "pregunta": "¿Cuál es el plazo de entrega?",
        "contratos_relevantes": [11], "hechos_requeridos": ["30 días"], "fuentes": FUENTE,
        "revision": APROBADA,
    }
    return Caso.model_validate({**base, **over})


def obs(**over) -> Observacion:
    return Observacion.model_validate({
        "caso_id": "c1", "http_status": 200, "contratos_devueltos": [11],
        "respuesta": "El plazo de entrega es de 30 días calendario.", **over,
    })


class TestValidacionDeCasos:
    def test_aprobado_exige_revisor_y_fecha(self):
        with pytest.raises(ValidationError, match="revisor y fecha"):
            caso(revision={"estado": "aprobado"})
        with pytest.raises(ValidationError, match="revisor y fecha"):
            caso(revision={"estado": "aprobado", "revisor": "  ", "fecha": "2026-10-10"})

    def test_respuesta_y_recuperacion_exigen_tdr_real_y_relevantes(self):
        with pytest.raises(ValidationError, match="fuente de TDR real"):
            caso(fuentes=[])
        with pytest.raises(ValidationError, match="contratos_relevantes"):
            caso(tipo="recuperacion", contratos_relevantes=[])
        with pytest.raises(ValidationError, match="hechos_requeridos"):
            caso(hechos_requeridos=[])

    def test_sin_respuesta_no_declara_esperados(self):
        with pytest.raises(ValidationError, match="sin_respuesta"):
            caso(tipo="sin_respuesta")
        assert caso(tipo="sin_respuesta", contratos_relevantes=[], hechos_requeridos=[], fuentes=[]).tipo == "sin_respuesta"

    def test_dataset_rechaza_ids_repetidos(self, tmp_path):
        un_caso = caso().model_dump(mode="json")
        ruta = tmp_path / "dataset.json"
        ruta.write_text(json.dumps({"casos": [un_caso, un_caso]}), encoding="utf-8")
        with pytest.raises(ValueError, match="repetido"):
            cargar_dataset(ruta)

    def test_dataset_vacio_es_valido_y_no_evalua_nada(self, tmp_path):
        ruta = tmp_path / "dataset.json"
        ruta.write_text('{"casos": []}', encoding="utf-8")
        assert evaluar(cargar_dataset(ruta), [], k=5) == []


class TestPuntuacion:
    def test_respuesta_correcta_con_tildes_y_mayusculas_distintas(self):
        r = puntuar(caso(hechos_requeridos=["Treinta DÍAS"]), obs(respuesta="Son treinta dias."), k=5)
        assert (r.veredicto, r.cobertura_hechos, r.recall_en_k, r.rango_reciproco) == ("ok", 1.0, 1.0, 1.0)

    def test_http_200_con_respuesta_convincente_pero_sin_el_hecho_falla(self):
        r = puntuar(caso(), obs(respuesta="El plazo está claramente definido en el documento."), k=5)
        assert (r.veredicto, r.cobertura_hechos, r.motivo) == ("falla", 0.0, "faltan hechos requeridos en la respuesta")

    def test_hecho_correcto_sobre_el_contrato_equivocado_falla(self):
        r = puntuar(caso(), obs(contratos_devueltos=[99, 98]), k=5)
        assert (r.veredicto, r.recall_en_k, r.motivo) == ("falla", 0.0, "no recuperó un contrato relevante")

    def test_hecho_prohibido_invalida_aunque_el_resto_acierte(self):
        r = puntuar(caso(hechos_prohibidos=["60 días"]), obs(respuesta="30 días, aunque también dice 60 días."), k=5)
        assert (r.veredicto, r.hechos_prohibidos_presentes) == ("falla", ["60 días"])

    def test_recuperacion_mide_posicion_y_corte_k(self):
        c = caso(tipo="recuperacion", contratos_relevantes=[11, 12], hechos_requeridos=[])
        dentro = puntuar(c, obs(contratos_devueltos=[7, 12, 8, 11]), k=3)
        assert (dentro.veredicto, dentro.recall_en_k, dentro.rango_reciproco) == ("ok", 0.5, 0.5)
        fuera = puntuar(c, obs(contratos_devueltos=[7, 8, 9, 11]), k=3)
        assert (fuera.veredicto, fuera.recall_en_k, fuera.rango_reciproco) == ("falla", 0.0, 0.25)

    def test_sin_respuesta_exige_abstencion(self):
        c = caso(tipo="sin_respuesta", contratos_relevantes=[], hechos_requeridos=[], fuentes=[], hechos_prohibidos=["S/ 50,000"])
        assert puntuar(c, obs(contratos_devueltos=[], respuesta="No encontré información sobre eso."), k=5).veredicto == "ok"
        assert puntuar(c, obs(contratos_devueltos=[11], respuesta="No encontré información."), k=5).veredicto == "falla"
        assert puntuar(c, obs(contratos_devueltos=[], respuesta="El monto es S/ 50,000."), k=5).veredicto == "falla"

    def test_error_y_ausencia_no_cuentan_como_falla_del_modelo(self):
        assert puntuar(caso(), obs(http_status=503), k=5).veredicto == "error"
        assert puntuar(caso(), obs(error="timeout"), k=5).motivo == "timeout"
        assert puntuar(caso(), None, k=5).veredicto == "sin_observacion"


class TestCorrida:
    def test_solo_se_evaluan_casos_aprobados(self):
        casos = [caso(), caso(id="c2", revision={"estado": "pendiente"}), caso(id="c3", revision={"estado": "rechazado"})]
        resultados = evaluar(casos, [obs(), obs(caso_id="c2"), obs(caso_id="inexistente")], k=5)
        assert [r.caso_id for r in resultados] == ["c1"]

    def test_resumen_sin_umbral_no_declara_cumplimiento(self):
        resultados = evaluar([caso(), caso(id="c2")], [obs(), obs(caso_id="c2", respuesta="no sé")], k=5)
        r = resumen(resultados)
        assert (r["casos"], r["ok"], r["falla"], r["tasa_ok"], r["cumple"]) == (2, 1, 1, 0.5, None)
        assert resumen(resultados, umbral=0.5)["cumple"] is True
        assert resumen(resultados, umbral=0.8)["cumple"] is False
        assert resumen([], umbral=0.8)["cumple"] is None

    def test_registro_identifica_versiones_y_dataset(self):
        casos = [caso()]
        corrida = Corrida(fecha=date(2026, 10, 10), corpus_espacio="qwen-tev4-1536", modelo="qwen3.7-flash", prompt_version="1.qwen.a3815aa6", config_version=47)
        reg = registro_corrida(corrida, casos, evaluar(casos, [obs()], k=corrida.k), umbral=0.9)
        assert reg["corrida"]["modelo"] == "qwen3.7-flash"
        assert reg["dataset_sha256"] == huella_dataset(casos)
        assert reg["resumen"]["cumple"] is True
        json.dumps(reg)

    def test_huella_cambia_con_el_contenido_y_no_con_casos_sin_aprobar(self):
        base = huella_dataset([caso()])
        assert huella_dataset([caso(hechos_requeridos=["45 días"])]) != base
        casos = [caso(), caso(id="c2", revision={"estado": "pendiente"})]
        corrida = Corrida(fecha=date(2026, 10, 10), corpus_espacio="e", modelo="m", prompt_version="p")
        assert registro_corrida(corrida, casos, [])["dataset_sha256"] == base
