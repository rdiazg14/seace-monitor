#!/usr/bin/env python3
"""Prueba camino Actions: escribir_keyword sin DATABASE_URL via supabase."""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_ENV = _ROOT / ".env"
if _ENV.is_file():
    for line in _ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# Simular Actions sin DSN
os.environ.pop("DATABASE_URL", None)

from supabase import create_client  # noqa: E402
from clasificacion_capa import (  # noqa: E402
    conectar_pg,
    diff_ids_supa,
    escribir_keyword,
)


def main() -> int:
    assert conectar_pg() is None, "DATABASE_URL no debia estar"
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    supa = create_client(url, key)

    rows = (
        supa.table("clasificacion_contrato")
        .select("contrato_id,categoria_it,capa")
        .eq("capa", "keyword")
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        print("ERROR: no hay filas capa=keyword", flush=True)
        return 1
    cid = int(rows[0]["contrato_id"])
    cat0 = rows[0]["categoria_it"]
    alt = "Software" if cat0 != "Software" else "Hardware"
    print(f"test_id={cid} cat0={cat0} alt={alt}", flush=True)

    n, s = escribir_keyword(
        [{"contrato_id": cid, "categoria_it": alt}],
        artefacto="test_supa_fallback",
        supa=supa,
    )
    print(f"write1 escritos={n} saltados={s}", flush=True)
    c = (
        supa.table("contratos")
        .select("id,categoria_it")
        .eq("id", cid)
        .single()
        .execute()
        .data
    )
    cl = (
        supa.table("clasificacion_contrato")
        .select("contrato_id,categoria_it,capa")
        .eq("contrato_id", cid)
        .single()
        .execute()
        .data
    )
    print(
        f"after contratos={c['categoria_it']} clasif={cl['categoria_it']} "
        f"capa={cl['capa']}",
        flush=True,
    )
    assert c["categoria_it"] == alt == cl["categoria_it"]
    assert diff_ids_supa(supa, [cid]) == 0

    n, s = escribir_keyword(
        [{"contrato_id": cid, "categoria_it": cat0}],
        artefacto="test_supa_fallback",
        supa=supa,
    )
    print(f"revert escritos={n} saltados={s}", flush=True)
    c = (
        supa.table("contratos")
        .select("id,categoria_it")
        .eq("id", cid)
        .single()
        .execute()
        .data
    )
    cl = (
        supa.table("clasificacion_contrato")
        .select("contrato_id,categoria_it,capa")
        .eq("contrato_id", cid)
        .single()
        .execute()
        .data
    )
    print(
        f"after_revert contratos={c['categoria_it']} clasif={cl['categoria_it']}",
        flush=True,
    )
    assert c["categoria_it"] == cat0 == cl["categoria_it"]
    assert diff_ids_supa(supa, [cid]) == 0
    print("OK supabase fallback + eco", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
