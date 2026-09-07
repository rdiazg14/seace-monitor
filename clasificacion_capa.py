#!/usr/bin/env python3
"""Helpers capa 3: escritura en clasificacion_contrato + diff vs contratos.

Fase 4: los escritores dejan de tocar contratos.categoria_it; el trigger
trg_clasificacion_echo copia a contratos. Keywords no pisan gemini/humano;
Gemini no pisa humano.

Escritura: preferir psycopg (DATABASE_URL). En GitHub Actions el secret
puede faltar: fallback a supabase-py (service role) que tambien dispara el eco.
"""
from __future__ import annotations

import os
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


def diff_ids_supa(supa, ids: list[int]) -> int:
    """Diff solo sobre ids tocados (camino Actions sin DATABASE_URL)."""
    if not ids:
        return 0
    n = 0
    for i in range(0, len(ids), 200):
        chunk = ids[i: i + 200]
        c_rows = (
            supa.table("contratos")
            .select("id,categoria_it,relevancia_ia")
            .in_("id", chunk)
            .execute()
            .data
            or []
        )
        cl_rows = (
            supa.table("clasificacion_contrato")
            .select("contrato_id,categoria_it,relevancia_ia")
            .in_("contrato_id", chunk)
            .execute()
            .data
            or []
        )
        by_c = {int(r["id"]): r for r in c_rows}
        by_cl = {int(r["contrato_id"]): r for r in cl_rows}
        for cid in chunk:
            a = by_c.get(cid) or {}
            b = by_cl.get(cid) or {}
            if cid not in by_c and cid not in by_cl:
                continue
            if (
                a.get("categoria_it") != b.get("categoria_it")
                or a.get("relevancia_ia") != b.get("relevancia_ia")
            ):
                n += 1
    return n


def upsert_keyword(
    conn,
    filas: list[dict[str, Any]],
    *,
    artefacto: str | None = None,
) -> tuple[int, int]:
    """Escribe capa=keyword. No pisa gemini/humano. Requiere psycopg conn."""
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
    """Escribe capa=gemini. No pisa humano. Requiere psycopg conn."""
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


def upsert_keyword_supa(
    supa,
    filas: list[dict[str, Any]],
    *,
    artefacto: str | None = None,
) -> tuple[int, int]:
    """Misma regla que upsert_keyword, via PostgREST (dispara el eco)."""
    if not filas:
        return 0, 0
    ids = [int(f["contrato_id"]) for f in filas]
    prev_map: dict[int, dict] = {}
    for i in range(0, len(ids), 200):
        chunk = ids[i: i + 200]
        rows = (
            supa.table("clasificacion_contrato")
            .select("contrato_id,capa,categoria_it,relevancia_ia")
            .in_("contrato_id", chunk)
            .execute()
            .data
            or []
        )
        for r in rows:
            prev_map[int(r["contrato_id"])] = r

    escritos = 0
    saltados = 0
    upserts: list[dict] = []
    deletes: list[int] = []
    for f in filas:
        cid = int(f["contrato_id"])
        cat = f.get("categoria_it")
        ia = f.get("relevancia_ia")
        kid = f.get("keyword_id")
        prev = prev_map.get(cid)
        if prev:
            if prev.get("capa") in CAPAS_PROTEGIDAS_KW:
                saltados += 1
                continue
            if "relevancia_ia" not in f:
                ia = prev.get("relevancia_ia")
            if "categoria_it" not in f:
                cat = prev.get("categoria_it")
        if cat is None and ia is None:
            if prev and prev.get("capa") == "keyword":
                deletes.append(cid)
                escritos += 1
            continue
        upserts.append({
            "contrato_id": cid,
            "categoria_it": cat,
            "relevancia_ia": ia,
            "capa": "keyword",
            "keyword_id": kid,
            "consenso_n": 0,
            "artefacto": artefacto,
        })
        escritos += 1

    for cid in deletes:
        (
            supa.table("clasificacion_contrato")
            .delete()
            .eq("contrato_id", cid)
            .eq("capa", "keyword")
            .execute()
        )
    for i in range(0, len(upserts), 100):
        lote = upserts[i: i + 100]
        # Protegidos ya filtrados; upsert pisa solo keyword/inexistente.
        supa.table("clasificacion_contrato").upsert(
            lote, on_conflict="contrato_id"
        ).execute()
    return escritos, saltados


def upsert_gemini_supa(supa, filas: list[dict[str, Any]]) -> tuple[int, int]:
    """Misma regla que upsert_gemini, via PostgREST."""
    if not filas:
        return 0, 0
    ids = [int(f["contrato_id"]) for f in filas]
    prev_map: dict[int, dict] = {}
    for i in range(0, len(ids), 200):
        chunk = ids[i: i + 200]
        rows = (
            supa.table("clasificacion_contrato")
            .select("contrato_id,capa,relevancia_ia")
            .in_("contrato_id", chunk)
            .execute()
            .data
            or []
        )
        for r in rows:
            prev_map[int(r["contrato_id"])] = r

    escritos = 0
    saltados = 0
    upserts: list[dict] = []
    for f in filas:
        cid = int(f["contrato_id"])
        cat = f.get("categoria_it")
        if not cat:
            continue
        prev = prev_map.get(cid)
        if prev and prev.get("capa") in CAPAS_PROTEGIDAS_GEMINI:
            saltados += 1
            continue
        ia = prev.get("relevancia_ia") if prev else None
        upserts.append({
            "contrato_id": cid,
            "categoria_it": cat,
            "relevancia_ia": ia,
            "capa": "gemini",
            "senal": f.get("senal"),
            "senal_fuente": f.get("senal_fuente"),
            "confianza": f.get("confianza"),
            "consenso_n": int(f.get("consenso_n") or 0),
            "revisar": bool(f.get("revisar") or False),
            "artefacto": f.get("artefacto"),
        })
        escritos += 1

    for i in range(0, len(upserts), 100):
        lote = upserts[i: i + 100]
        # Humanos filtrados arriba; upsert keyword→gemini OK.
        supa.table("clasificacion_contrato").upsert(
            lote, on_conflict="contrato_id"
        ).execute()
    return escritos, saltados


def map_confianza(raw: str | None) -> float | None:
    if not raw:
        return None
    m = {"alta": 3.0, "media": 2.0, "baja": 1.0}
    return m.get(str(raw).strip().lower())


def conectar_pg():
    """None si no hay DATABASE_URL."""
    dsn = (os.getenv("DATABASE_URL") or "").strip()
    if not dsn:
        return None
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, row_factory=dict_row)


def anunciar_backend_capa3(*, supa=None) -> str:
    """Log explicito del camino de escritura (psycopg vs supabase-py)."""
    if (os.getenv("DATABASE_URL") or "").strip():
        msg = "[clasificacion] backend=psycopg (DATABASE_URL presente)"
        print(msg, flush=True)
        return "psycopg"
    if supa is not None:
        msg = (
            "[clasificacion] backend=supabase-py "
            "(DATABASE_URL ausente; fallback Actions)"
        )
        print(msg, flush=True)
        return "supabase-py"
    raise RuntimeError(
        "capa 3: falta DATABASE_URL y cliente supabase"
    )


def escribir_keyword(
    filas: list[dict[str, Any]],
    *,
    artefacto: str | None = None,
    supa=None,
) -> tuple[int, int]:
    """Preferir psycopg; si no hay DSN, supabase-py (Actions)."""
    if not filas:
        return 0, 0
    conn = conectar_pg()
    if conn is not None:
        try:
            conn.autocommit = False
            n, s = upsert_keyword(conn, filas, artefacto=artefacto)
            diff = diff_clasificacion_contratos(conn)
            if diff != 0:
                conn.rollback()
                raise RuntimeError(
                    f"diff clasificacion/contratos={diff} tras keyword"
                )
            conn.commit()
            return n, s
        finally:
            conn.close()
    if supa is None:
        raise RuntimeError(
            "capa 3 keyword: falta DATABASE_URL y cliente supabase"
        )
    print(
        "[clasificacion] DATABASE_URL ausente; escribiendo capa 3 via "
        "supabase-py (fallback Actions)",
        flush=True,
    )
    n, s = upsert_keyword_supa(supa, filas, artefacto=artefacto)
    ids = [int(f["contrato_id"]) for f in filas]
    diff = diff_ids_supa(supa, ids)
    if diff != 0:
        raise RuntimeError(
            f"diff ids tocados clasificacion/contratos={diff} tras keyword"
        )
    return n, s


def escribir_gemini(
    filas: list[dict[str, Any]],
    *,
    supa=None,
) -> tuple[int, int]:
    """Preferir psycopg; si no hay DSN, supabase-py (Actions)."""
    if not filas:
        return 0, 0
    conn = conectar_pg()
    if conn is not None:
        try:
            conn.autocommit = False
            n, s = upsert_gemini(conn, filas)
            diff = diff_clasificacion_contratos(conn)
            if diff != 0:
                conn.rollback()
                raise RuntimeError(
                    f"diff clasificacion/contratos={diff} tras gemini"
                )
            conn.commit()
            return n, s
        finally:
            conn.close()
    if supa is None:
        raise RuntimeError(
            "capa 3 gemini: falta DATABASE_URL y cliente supabase"
        )
    print(
        "[clasificacion] DATABASE_URL ausente; escribiendo capa 3 via "
        "supabase-py (fallback Actions)",
        flush=True,
    )
    n, s = upsert_gemini_supa(supa, filas)
    ids = [int(f["contrato_id"]) for f in filas]
    diff = diff_ids_supa(supa, ids)
    if diff != 0:
        raise RuntimeError(
            f"diff ids tocados clasificacion/contratos={diff} tras gemini"
        )
    return n, s
