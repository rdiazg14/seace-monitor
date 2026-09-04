#!/usr/bin/env python3
"""Verifica que it_keywords reproduce clasificar_categoria_it (IT_CATS).

Lee la tabla, aplica la cascada (prioridad, incluye/excluye, limite_palabra)
sobre todos los contratos y compara contra el codigo. No escribe en Supabase.
"""
from __future__ import annotations

import os
import re
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from ingesta_completa import _norm, _texto_contrato, clasificar_categoria_it  # noqa: E402

_ENV = _ROOT / ".env"


def _cargar_env() -> None:
    if not _ENV.exists():
        return
    for line in _ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _contiene(texto_norm: str, kw: str, limite_palabra: bool) -> bool:
    kn = _norm(kw)
    if limite_palabra:
        return bool(re.search(r"\b" + re.escape(kn) + r"\b", texto_norm))
    return kn in texto_norm


def _fila_api(row: dict) -> dict:
    return {
        "desObjetoContrato": row["descripcion"],
        "desContratacion": row["descripcion_contrato"],
        "nomObjetoContrato": row["objeto"],
        "nomEntidad": row["entidad"],
    }


def _cargar_cascada(conn) -> list[tuple[int, str, list[tuple[str, bool]], list[tuple[str, bool]]]]:
    """[(prioridad, categoria, includes, excludes), ...] ordenado por prioridad."""
    rows = conn.execute(
        """
        SELECT categoria, keyword, prioridad, tipo, limite_palabra
        FROM it_keywords
        WHERE activa
        ORDER BY prioridad, id
        """
    ).fetchall()
    por_cat: dict[str, dict] = {}
    for r in rows:
        cat = r["categoria"]
        slot = por_cat.setdefault(
            cat,
            {
                "prioridad": int(r["prioridad"]),
                "incluye": [],
                "excluye": [],
            },
        )
        item = (r["keyword"], bool(r["limite_palabra"]))
        if r["tipo"] == "excluye":
            slot["excluye"].append(item)
        else:
            slot["incluye"].append(item)
    cascada = [
        (v["prioridad"], cat, v["incluye"], v["excluye"])
        for cat, v in por_cat.items()
    ]
    cascada.sort(key=lambda x: (x[0], x[1]))
    return cascada


def clasificar_tabla(api: dict, cascada) -> str | None:
    t = _texto_contrato(api)
    for _pri, cat, incluye, excluye in cascada:
        if any(_contiene(t, kw, lim) for kw, lim in excluye):
            continue
        if any(_contiene(t, kw, lim) for kw, lim in incluye):
            return cat
    return None


def main() -> int:
    _cargar_env()
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("ERROR: falta DATABASE_URL en el entorno o en .env", flush=True)
        return 2

    import psycopg
    from psycopg.rows import dict_row

    try:
        with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as conn:
            cascada = _cargar_cascada(conn)
            n_kw = conn.execute(
                "SELECT count(*) AS n FROM it_keywords WHERE activa"
            ).fetchone()["n"]
            contratos = conn.execute(
                """
                SELECT id, descripcion, descripcion_contrato, objeto, entidad
                FROM contratos
                ORDER BY id
                """
            ).fetchall()
    except Exception as e:
        msg = str(e).replace(dsn, "***")
        print(f"ERROR {type(e).__name__}: {msg}", file=sys.stderr, flush=True)
        return 1

    print(
        f"keywords_activas={n_kw} categorias_cascada={len(cascada)} "
        f"contratos={len(contratos)}",
        flush=True,
    )

    ok = 0
    disc: list[tuple[int, str | None, str | None]] = []
    for row in contratos:
        api = _fila_api(row)
        codigo = clasificar_categoria_it(api)
        tabla = clasificar_tabla(api, cascada)
        if codigo == tabla:
            ok += 1
        else:
            disc.append((int(row["id"]), codigo, tabla))

    total = len(contratos)
    print(f"total={total} coincidencias={ok} discrepancias={len(disc)}", flush=True)
    if disc:
        print("--- discrepancias (id, codigo, tabla) ---", flush=True)
        for i, codigo, tabla in disc:
            print(f"  {i}\tcodigo={codigo!r}\ttabla={tabla!r}", flush=True)
        return 1
    print("EQUIVALENCIA OK: 0 discrepancias.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
