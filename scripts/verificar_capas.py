#!/usr/bin/env python3
"""Coherencia capa 3 vs contratos. Read-only. Pensado para Actions (sin DSN).

Imprime: [capas] diff=N capa_null=N huerfanos=N c1=N
Exit 1 si diff > 0 (G3).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_ENV = _ROOT / ".env"
if _ENV.is_file():
    for line in _ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

from clasificacion_capa import conectar_pg, diff_clasificacion_contratos  # noqa: E402

IDS_C1 = [
    273, 10353, 11435, 11988, 12399, 20626, 32171, 32378, 34382, 34492,
    35576, 35751, 36445, 36973, 40586, 43667, 46129, 50908, 55367, 57244,
    57871, 57882, 58672, 59934, 63954, 65580, 65997, 66279, 67658, 68477,
    70601, 70826, 72158, 72867, 74482, 77609, 77999, 79918, 84043, 85541,
    88126, 90076, 90342, 90815, 90819, 90832, 90869, 90875, 90891, 91148,
    91197, 91221, 91321, 91342,
]
OUT = _ROOT / "data" / "ultima_capas.txt"
PAGE = 1000


def _init_supa():
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()
    if not url or not key:
        return None
    from supabase import create_client
    return create_client(url, key)


def _paginar(supa, table: str, select: str, **eq) -> list[dict]:
    out: list[dict] = []
    offset = 0
    q0 = supa.table(table).select(select)
    for k, v in eq.items():
        if v is None:
            q0 = q0.is_(k, "null")
        else:
            q0 = q0.eq(k, v)
    while True:
        q = supa.table(table).select(select)
        for k, v in eq.items():
            if v is None:
                q = q.is_(k, "null")
            else:
                q = q.eq(k, v)
        res = q.range(offset, offset + PAGE - 1).execute()
        batch = res.data or []
        out.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return out


def via_pg() -> tuple[int, int, int, int] | None:
    conn = conectar_pg()
    if conn is None:
        return None
    try:
        diff = diff_clasificacion_contratos(conn)
        capa_null = conn.execute(
            "SELECT count(*)::int AS n FROM clasificacion_contrato WHERE capa IS NULL"
        ).fetchone()["n"]
        huerfanos = conn.execute(
            """
            SELECT count(*)::int AS n
            FROM contratos c
            LEFT JOIN clasificacion_contrato cl ON cl.contrato_id = c.id
            WHERE (c.categoria_it IS NOT NULL OR c.relevancia_ia IS NOT NULL)
              AND cl.contrato_id IS NULL
            """
        ).fetchone()["n"]
        c1 = conn.execute(
            "SELECT count(*)::int AS n FROM clasificacion_contrato "
            "WHERE capa = 'gemini' AND contrato_id = ANY(%s)",
            (IDS_C1,),
        ).fetchone()["n"]
        return int(diff), int(capa_null), int(huerfanos), int(c1)
    finally:
        conn.close()


def via_supa(supa) -> tuple[int, int, int, int]:
    clasif = _paginar(supa, "clasificacion_contrato",
                      "contrato_id,categoria_it,relevancia_ia,capa")
    contratos: list[dict] = []
    offset = 0
    while True:
        res = (
            supa.table("contratos")
            .select("id,categoria_it,relevancia_ia")
            .or_("categoria_it.not.is.null,relevancia_ia.not.is.null")
            .range(offset, offset + PAGE - 1)
            .execute()
        )
        batch = res.data or []
        contratos.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE

    by_cl = {int(r["contrato_id"]): r for r in clasif}
    by_c = {int(r["id"]): r for r in contratos}
    ids = set(by_cl) | set(by_c)
    diff = 0
    for cid in ids:
        a = by_c.get(cid) or {}
        b = by_cl.get(cid) or {}
        if (
            a.get("categoria_it") != b.get("categoria_it")
            or a.get("relevancia_ia") != b.get("relevancia_ia")
        ):
            diff += 1
    capa_null = sum(1 for r in clasif if r.get("capa") is None)
    huerfanos = sum(1 for cid in by_c if cid not in by_cl)
    c1 = sum(
        1 for cid in IDS_C1
        if by_cl.get(cid, {}).get("capa") == "gemini"
    )
    return diff, capa_null, huerfanos, c1


def persistir(linea: str) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    run = os.getenv("GITHUB_RUN_ID") or "-"
    OUT.write_text(
        f"ts={ts}\nrun_id={run}\n{linea}\n",
        encoding="utf-8",
    )


def main() -> int:
    nums = via_pg()
    origen = "psycopg"
    if nums is None:
        supa = _init_supa()
        if supa is None:
            print("ERROR: falta DATABASE_URL y SUPABASE_SERVICE_KEY", flush=True)
            return 2
        nums = via_supa(supa)
        origen = "supabase-py"
    diff, capa_null, huerfanos, c1 = nums
    linea = (
        f"[capas] diff={diff} capa_null={capa_null} "
        f"huerfanos={huerfanos} c1={c1}"
    )
    print(linea, flush=True)
    print(f"[capas] backend={origen}", flush=True)
    persistir(linea)
    return 1 if diff > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
