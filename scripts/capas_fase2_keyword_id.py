#!/usr/bin/env python3
"""Capas fase 2: rellenar keyword_id en clasificacion_contrato.

La cascada ACTUAL no es la que produjo las etiquetas historicas. Solo
escribe keyword_id cuando clasificar_con_kw reproduce exactamente
contratos.categoria_it (y capa='keyword'). Si no coincide, deja NULL.

No toca contratos. No cambia capa/artefacto/consenso.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from backfill_categoria import (  # noqa: E402
    IDS_C1_HARDCODE,
    IDS_PROTEGIDOS,
    _fila_api,
    _match_kw,
)
from ingesta_completa import _texto_contrato  # noqa: E402

_ENV = _ROOT / ".env"


def _cargar_env() -> None:
    if not _ENV.exists():
        return
    for line in _ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _dsn() -> str:
    _cargar_env()
    dsn = (os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        print("ERROR: falta DATABASE_URL", flush=True)
        sys.exit(2)
    return dsn


def _cargar_cascada_con_id(conn) -> list[tuple[str, list[dict]]]:
    filas = conn.execute(
        """
        SELECT id, categoria, keyword, tipo, limite_palabra, prioridad, tolera_plural
        FROM it_keywords
        WHERE activa
        ORDER BY prioridad, id
        """
    ).fetchall()
    grupos: dict[str, list[dict]] = {}
    for f in filas:
        grupos.setdefault(f["categoria"], []).append({
            "id": int(f["id"]),
            "keyword": f["keyword"],
            "tipo": f.get("tipo") or "incluye",
            "limite_palabra": bool(f.get("limite_palabra")),
            "tolera_plural": bool(f.get("tolera_plural")),
        })
    return list(grupos.items())


def clasificar_con_kw_id(
    api: dict,
    cats: list[tuple[str, list[dict]]],
) -> tuple[str | None, int | None, str | None]:
    """(categoria, keyword_id, keyword). Replica clasificar_con_kw + id."""
    t = _texto_contrato(api)
    for cat, kws in cats:
        if any(_match_kw(t, d) for d in kws if d.get("tipo") == "excluye"):
            continue
        for d in kws:
            if d.get("tipo") == "excluye":
                continue
            if _match_kw(t, d):
                return cat, int(d["id"]), d["keyword"]
    return None, None, None


def main() -> int:
    protegidos = set(IDS_C1_HARDCODE) | set(IDS_PROTEGIDOS)
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        cats = _cargar_cascada_con_id(conn)
        rows = conn.execute(
            """
            SELECT
              cl.contrato_id AS id,
              c.categoria_it AS cat_c,
              c.descripcion,
              c.descripcion_contrato,
              c.objeto,
              c.entidad
            FROM clasificacion_contrato cl
            JOIN contratos c ON c.id = cl.contrato_id
            WHERE cl.capa = 'keyword'
              AND cl.categoria_it IS NOT NULL
            ORDER BY cl.contrato_id
            """
        ).fetchall()

        updates: list[tuple[int, int]] = []
        n_ok = 0
        n_mismatch = 0
        n_sin_match = 0
        mismatch_ej: list[tuple[int, str | None, str | None, str | None]] = []

        for r in rows:
            cid = int(r["id"])
            if cid in protegidos:
                continue
            api = _fila_api(r)
            cat, kid, kw = clasificar_con_kw_id(api, cats)
            esperado = r["cat_c"]
            if cat is None:
                n_sin_match += 1
                continue
            if cat != esperado:
                n_mismatch += 1
                if len(mismatch_ej) < 10:
                    mismatch_ej.append((cid, esperado, cat, kw))
                continue
            updates.append((kid, cid))
            n_ok += 1

        with conn.cursor() as cur:
            cur.executemany(
                """
                UPDATE clasificacion_contrato
                SET keyword_id = %s
                WHERE contrato_id = %s
                  AND capa = 'keyword'
                  AND keyword_id IS NULL
                """,
                updates,
            )
        conn.commit()

        n_con = conn.execute(
            "SELECT count(*)::int AS n FROM clasificacion_contrato "
            "WHERE keyword_id IS NOT NULL"
        ).fetchone()["n"]
        n_null = conn.execute(
            "SELECT count(*)::int AS n FROM clasificacion_contrato "
            "WHERE keyword_id IS NULL"
        ).fetchone()["n"]
        n_kw_capa = conn.execute(
            "SELECT count(*)::int AS n FROM clasificacion_contrato "
            "WHERE capa = 'keyword'"
        ).fetchone()["n"]

        print("--- capas_fase2_keyword_id ---", flush=True)
        print(f"  candidatos keyword con cat: {len(rows)}", flush=True)
        print(f"  keyword_id escritos (reproducen cat): {n_ok}", flush=True)
        print(f"  cascada no reproduce cat (NULL): {n_mismatch}", flush=True)
        print(f"  cascada sin match (NULL): {n_sin_match}", flush=True)
        print(f"  tabla keyword_id NOT NULL: {n_con}", flush=True)
        print(f"  tabla keyword_id NULL: {n_null} (gemini+solo_ia+no_resueltos)", flush=True)
        print(f"  capa keyword: {n_kw_capa}", flush=True)
        if mismatch_ej:
            print("  ejemplos mismatch (id, cat_bd, cat_cascada, kw):", flush=True)
            for ej in mismatch_ej:
                print(f"    {ej}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
