"""Dataset de calidad y puntuación local de respuestas del asistente (GW-006).

Este módulo no llama a ningún proveedor ni a la base de datos: valida casos,
puntúa observaciones ya capturadas y deja un registro comparable entre
corridas. Reglas que fija:

- Solo cuentan los casos con revisión humana aprobada (revisor y fecha).
- Un HTTP 200 no aprueba nada: cada tipo de caso tiene su propio criterio.
- La cobertura de hechos es una comprobación léxica; ayuda a comparar corridas
  pero no reemplaza la lectura humana de la respuesta.
- Sin umbral acordado el resumen no declara cumplimiento.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

TipoCaso = Literal["recuperacion", "respuesta", "sin_respuesta"]
Veredicto = Literal["ok", "falla", "error", "sin_observacion"]


class Revision(BaseModel):
    estado: Literal["pendiente", "aprobado", "rechazado"] = "pendiente"
    revisor: str | None = None
    fecha: date | None = None

    @model_validator(mode="after")
    def _aprobado_con_responsable(self) -> "Revision":
        if self.estado == "aprobado" and (not (self.revisor or "").strip() or self.fecha is None):
            raise ValueError("una revisión aprobada requiere revisor y fecha")
        return self


class FuenteTdr(BaseModel):
    """TDR real del que sale el caso: el análisis no se sustenta en la ficha."""

    contrato_id: int = Field(gt=0)
    pdf_hash: str = Field(min_length=1)


class Caso(BaseModel):
    id: str = Field(min_length=1)
    tipo: TipoCaso
    pregunta: str = Field(min_length=1)
    contratos_relevantes: list[int] = Field(default_factory=list)
    hechos_requeridos: list[str] = Field(default_factory=list)
    hechos_prohibidos: list[str] = Field(default_factory=list)
    fuentes: list[FuenteTdr] = Field(default_factory=list)
    revision: Revision = Field(default_factory=Revision)

    @model_validator(mode="after")
    def _coherente_con_tipo(self) -> "Caso":
        if self.tipo == "sin_respuesta":
            if self.contratos_relevantes or self.hechos_requeridos:
                raise ValueError("un caso sin_respuesta no declara contratos ni hechos esperados")
            return self
        if not self.contratos_relevantes:
            raise ValueError(f"un caso de {self.tipo} necesita contratos_relevantes")
        if not self.fuentes:
            raise ValueError(f"un caso de {self.tipo} necesita al menos una fuente de TDR real")
        if self.tipo == "respuesta" and not self.hechos_requeridos:
            raise ValueError("un caso de respuesta necesita hechos_requeridos")
        return self


class Observacion(BaseModel):
    """Lo que devolvió el asistente para un caso, capturado por quien ejecuta la corrida."""

    caso_id: str
    http_status: int | None = None
    contratos_devueltos: list[int] = Field(default_factory=list)
    respuesta: str | None = None
    error: str | None = None


class Corrida(BaseModel):
    """Identidad de una corrida: sin estos datos dos resultados no son comparables."""

    fecha: date
    corpus_espacio: str = Field(min_length=1)
    modelo: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    config_version: int | None = None
    k: int = Field(gt=0, default=5)


class Resultado(BaseModel):
    caso_id: str
    tipo: TipoCaso
    veredicto: Veredicto
    recall_en_k: float | None = None
    rango_reciproco: float | None = None
    cobertura_hechos: float | None = None
    hechos_prohibidos_presentes: list[str] = Field(default_factory=list)
    motivo: str | None = None


def cargar_dataset(path: Path) -> list[Caso]:
    """Lee y valida el dataset. Rechaza ids repetidos: dos casos no pueden pisarse."""
    crudo = json.loads(path.read_text(encoding="utf-8"))
    casos = [Caso.model_validate(c) for c in crudo.get("casos", [])]
    vistos: set[str] = set()
    for caso in casos:
        if caso.id in vistos:
            raise ValueError(f"id de caso repetido: {caso.id}")
        vistos.add(caso.id)
    return casos


def huella_dataset(casos: list[Caso]) -> str:
    """SHA-256 estable del contenido evaluable; cambia si cambia cualquier caso."""
    canon = json.dumps([c.model_dump(mode="json") for c in casos], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def casos_evaluables(casos: list[Caso]) -> list[Caso]:
    return [c for c in casos if c.revision.estado == "aprobado"]


def normalizar(texto: str) -> str:
    sin_tildes = "".join(ch for ch in unicodedata.normalize("NFD", texto) if unicodedata.category(ch) != "Mn")
    return " ".join(sin_tildes.casefold().split())


def recall_en_k(relevantes: list[int], devueltos: list[int], k: int) -> float:
    esperados = set(relevantes)
    return len(esperados & set(devueltos[:k])) / len(esperados)


def rango_reciproco(relevantes: list[int], devueltos: list[int]) -> float:
    esperados = set(relevantes)
    for posicion, contrato in enumerate(devueltos, start=1):
        if contrato in esperados:
            return 1 / posicion
    return 0.0


def _presentes(hechos: list[str], respuesta: str) -> list[str]:
    texto = normalizar(respuesta)
    return [h for h in hechos if normalizar(h) in texto]


def puntuar(caso: Caso, obs: Observacion | None, k: int) -> Resultado:
    base = {"caso_id": caso.id, "tipo": caso.tipo}
    if obs is None:
        return Resultado(**base, veredicto="sin_observacion", motivo="la corrida no incluyó este caso")
    if obs.error or obs.http_status != 200:
        return Resultado(**base, veredicto="error", motivo=obs.error or f"HTTP {obs.http_status}")
    respuesta = obs.respuesta or ""
    prohibidos = _presentes(caso.hechos_prohibidos, respuesta)

    if caso.tipo == "sin_respuesta":
        # El asistente no debe citar contratos ni afirmar lo que el caso prohíbe.
        ok = not obs.contratos_devueltos and not prohibidos
        motivo = None if ok else "citó contratos o afirmó contenido para una pregunta sin respuesta en el corpus"
        return Resultado(**base, veredicto="ok" if ok else "falla", hechos_prohibidos_presentes=prohibidos, motivo=motivo)

    recall = recall_en_k(caso.contratos_relevantes, obs.contratos_devueltos, k)
    rr = rango_reciproco(caso.contratos_relevantes, obs.contratos_devueltos)
    if caso.tipo == "recuperacion":
        ok = recall > 0
        return Resultado(
            **base, veredicto="ok" if ok else "falla", recall_en_k=recall, rango_reciproco=rr,
            motivo=None if ok else f"ningún contrato relevante en los primeros {k}",
        )

    cobertura = len(_presentes(caso.hechos_requeridos, respuesta)) / len(caso.hechos_requeridos)
    ok = recall > 0 and cobertura == 1 and not prohibidos
    motivo = None
    if not ok:
        motivo = (
            "afirma un hecho prohibido" if prohibidos
            else "no recuperó un contrato relevante" if recall == 0
            else "faltan hechos requeridos en la respuesta"
        )
    return Resultado(
        **base, veredicto="ok" if ok else "falla", recall_en_k=recall, rango_reciproco=rr,
        cobertura_hechos=cobertura, hechos_prohibidos_presentes=prohibidos, motivo=motivo,
    )


def evaluar(casos: list[Caso], observaciones: list[Observacion], k: int) -> list[Resultado]:
    """Puntúa solo los casos aprobados; una observación de más no crea un caso."""
    por_caso = {o.caso_id: o for o in observaciones}
    return [puntuar(c, por_caso.get(c.id), k) for c in casos_evaluables(casos)]


def resumen(resultados: list[Resultado], umbral: float | None = None) -> dict:
    """Agregado de la corrida. `cumple` es None mientras no haya umbral acordado o casos."""
    total = len(resultados)
    conteo = {v: sum(1 for r in resultados if r.veredicto == v) for v in ("ok", "falla", "error", "sin_observacion")}
    tasa = conteo["ok"] / total if total else None

    def media(valores: list[float]) -> float | None:
        return sum(valores) / len(valores) if valores else None

    return {
        "casos": total,
        **conteo,
        "tasa_ok": tasa,
        "recall_medio": media([r.recall_en_k for r in resultados if r.recall_en_k is not None]),
        "mrr": media([r.rango_reciproco for r in resultados if r.rango_reciproco is not None]),
        "cobertura_media": media([r.cobertura_hechos for r in resultados if r.cobertura_hechos is not None]),
        "umbral": umbral,
        "cumple": None if umbral is None or tasa is None else tasa >= umbral,
    }


def registro_corrida(corrida: Corrida, casos: list[Caso], resultados: list[Resultado], umbral: float | None = None) -> dict:
    """Registro autosuficiente para comparar corridas: versiones, huella del dataset y detalle."""
    return {
        "corrida": corrida.model_dump(mode="json"),
        "dataset_sha256": huella_dataset(casos_evaluables(casos)),
        "resumen": resumen(resultados, umbral),
        "resultados": [r.model_dump(mode="json") for r in resultados],
    }
