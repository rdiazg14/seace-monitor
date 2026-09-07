#!/usr/bin/env python3
"""Helpers capa 3: escritura en clasificacion_contrato + diff vs contratos.

Fase 4: los escritores dejan de tocar contratos.categoria_it; el trigger
trg_clasificacion_echo copia a contratos. Keywords no pisan gemini/humano;
Gemini no pisa humano.
"""
from __future__ import annotations

from typing import Any

CAPAS_PROTEGIDAS_KW = frozenset({"gemini", "humano"})
CAPAS_PROTEGIDAS_GEMINI = frozenset({"humano"})


def diff_clasificacion_contratos(conn) -> int:
    """Filas donde categoria_it o relevancia_ia difieren. 0 = sync."""
    row = conn.execute(
        """
        SELECT count(*)::int AS n
        FROM contratos c
        FULL OUTER JOIN clasificacion_contrato cl
          ON cl.contrato_id = c.id
        WHERE (
            c.categoria_it IS NOT NULL OR c.relevancia_ia IS NOT NULL
            OR cl.contrato_id IS NOT NULL
        )
        AND (
            c.categoria_it IS DISTINCT FROM cl.categoria_it
            OR c.relevancia_ia IS DISTINCT FROM cl.relevancia_ia
        )
        """
    ).fetchone()
    if isinstance(row, dict):
        return int(row["n"])
    return int(row[0])


def upsert_keyword(
    conn,
    filas: list[dict[str, Any]],
    *,
    artefacto: str | None = None,
) -> tuple[int, int]:
    """Escribe capa=keyword. No pisa gemini/humano.

    Cada dict: contrato_id, categoria_it?, relevancia_ia?, keyword_id?
    Si ambas etiquetas quedan NULL → DELETE (eco limpia contratos).
    Retorna (escritos, saltados_protegidos).
    """
    if not filas:
        return 0, 0
    escritos = 0
    saltados = 0
    for f in filas:
        cid = int(f["contrato_id"])
        cat = f.get("categoria_it")
        ia = f.get("relevancia_ia")
        kid = f.get("keyword_id")
        prev = conn.execute(
            "SELECT capa, categoria_it, relevancia_ia "
            "FROM clasificacion_contrato WHERE contrato_id = %s",
            (cid,),
        ).fetchone()
        if prev:
            capa = prev["capa"] if isinstance(prev, dict) else prev[0]
            if capa in CAPAS_PROTEGIDAS_KW:
                saltados += 1
                continue
            # Merge: si el caller no manda ia, conserva la vigente.
            if "relevancia_ia" not in f:
                ia = prev["relevancia_ia"] if isinstance(prev, dict) else prev[2]
            if "categoria_it" not in f:
                cat = prev["categoria_it"] if isinstance(prev, dict) else prev[1]

        if cat is None and ia is None:
            if prev:
                conn.execute(
                    "DELETE FROM clasificacion_contrato WHERE contrato_id = %s "
                    "AND capa = 'keyword'",
                    (cid,),
                )
                escritos += 1
            continue

        conn.execute(
            """
            INSERT INTO clasificacion_contrato (
              contrato_id, categoria_it, relevancia_ia, capa,
              keyword_id, consenso_n, artefacto
            ) VALUES (
              %(contrato_id)s, %(categoria_it)s, %(relevancia_ia)s, 'keyword',
              %(keyword_id)s, 0, %(artefacto)s
            )
            ON CONFLICT (contrato_id) DO UPDATE SET
              categoria_it = EXCLUDED.categoria_it,
              relevancia_ia = EXCLUDED.relevancia_ia,
              keyword_id = COALESCE(EXCLUDED.keyword_id, clasificacion_contrato.keyword_id),
              artefacto = COALESCE(EXCLUDED.artefacto, clasificacion_contrato.artefacto),
              actualizado_utc = now()
            WHERE clasificacion_contrato.capa = 'keyword'
            """,
            {
                "contrato_id": cid,
                "categoria_it": cat,
                "relevancia_ia": ia,
                "keyword_id": kid,
                "artefacto": artefacto,
            },
        )
        escritos += 1
    return escritos, saltados


def upsert_gemini(
    conn,
    filas: list[dict[str, Any]],
) -> tuple[int, int]:
    """Escribe capa=gemini. No pisa humano. No borra relevancia_ia ajena.

    Cada dict: contrato_id, categoria_it (required), senal?, senal_fuente?,
    confianza?, consenso_n?, revisar?, artefacto?
    """
    if not filas:
        return 0, 0
    escritos = 0
    saltados = 0
    for f in filas:
        cid = int(f["contrato_id"])
        cat = f.get("categoria_it")
        if not cat:
            continue
        prev = conn.execute(
            "SELECT capa, relevancia_ia FROM clasificacion_contrato "
            "WHERE contrato_id = %s",
            (cid,),
        ).fetchone()
        if prev:
            capa = prev["capa"] if isinstance(prev, dict) else prev[0]
            if capa in CAPAS_PROTEGIDAS_GEMINI:
                saltados += 1
                continue
            ia = prev["relevancia_ia"] if isinstance(prev, dict) else prev[1]
        else:
            ia = None

        conn.execute(
            """
            INSERT INTO clasificacion_contrato (
              contrato_id, categoria_it, relevancia_ia, capa,
              senal, senal_fuente, confianza, consenso_n, revisar, artefacto
            ) VALUES (
              %(contrato_id)s, %(categoria_it)s, %(relevancia_ia)s, 'gemini',
              %(senal)s, %(senal_fuente)s, %(confianza)s, %(consenso_n)s,
              %(revisar)s, %(artefacto)s
            )
            ON CONFLICT (contrato_id) DO UPDATE SET
              categoria_it = EXCLUDED.categoria_it,
              capa = 'gemini',
              senal = EXCLUDED.senal,
              senal_fuente = EXCLUDED.senal_fuente,
              confianza = EXCLUDED.confianza,
              consenso_n = EXCLUDED.consenso_n,
              revisar = EXCLUDED.revisar,
              artefacto = EXCLUDED.artefacto,
              actualizado_utc = now()
            WHERE clasificacion_contrato.capa <> 'humano'
            """,
            {
                "contrato_id": cid,
                "categoria_it": cat,
                "relevancia_ia": ia,
                "senal": f.get("senal"),
                "senal_fuente": f.get("senal_fuente"),
                "confianza": f.get("confianza"),
                "consenso_n": int(f.get("consenso_n") or 0),
                "revisar": bool(f.get("revisar") or False),
                "artefacto": f.get("artefacto"),
            },
        )
        escritos += 1
    return escritos, saltados


def map_confianza(raw: str | None) -> float | None:
    if not raw:
        return None
    m = {"alta": 3.0, "media": 2.0, "baja": 1.0}
    return m.get(str(raw).strip().lower())
