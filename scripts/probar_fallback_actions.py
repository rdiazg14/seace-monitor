#!/usr/bin/env python3
"""Prueba Actions: desetiquetar 1 keyword Vigente, o restaurar / verificar."""
from __future__ import annotations

import argparse
import json
import os
import sys
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

import psycopg
from psycopg.rows import dict_row

SNAP = _ROOT / "data" / "fase5b_smoke_desetiqueta.json"


def _cid(snap: dict, arg_id: int) -> int:
    return int(arg_id or snap.get("contrato_id") or snap.get("id"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pick", "desetiquetar", "status", "restore"])
    ap.add_argument("--id", type=int, default=0)
    args = ap.parse_args()

    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        if args.cmd == "pick":
            row = conn.execute(
                """
                SELECT cl.contrato_id AS id, cl.categoria_it, cl.relevancia_ia,
                       cl.capa, cl.keyword_id, cl.artefacto, c.estado,
                       left(coalesce(c.descripcion, ''), 80) AS desc
                FROM clasificacion_contrato cl
                JOIN contratos c ON c.id = cl.contrato_id
                WHERE cl.capa = 'keyword'
                  AND c.estado IN ('Vigente', 'En Evaluacion', 'En Evaluación')
                ORDER BY cl.contrato_id DESC
                LIMIT 5
                """
            ).fetchall()
            if not row:
                print("ERROR: sin candidato", flush=True)
                return 1
            # Preferir uno con keyword_id (cascada estable)
            chosen = next((r for r in row if r.get("keyword_id")), row[0])
            print(json.dumps(dict(chosen), default=str, ensure_ascii=False), flush=True)
            SNAP.write_text(
                json.dumps(dict(chosen), default=str, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print("snap=", SNAP, flush=True)
            return 0

        if args.cmd == "desetiquetar":
            snap = json.loads(SNAP.read_text(encoding="utf-8")) if SNAP.is_file() else {}
            cid = _cid(snap, args.id)
            prev = conn.execute(
                "SELECT * FROM clasificacion_contrato WHERE contrato_id = %s",
                (cid,),
            ).fetchone()
            if not prev:
                print("ERROR: sin fila clasificacion", flush=True)
                return 1
            if prev["capa"] != "keyword":
                print(f"ERROR: capa={prev['capa']} no es keyword; aborto", flush=True)
                return 1
            SNAP.write_text(
                json.dumps(dict(prev), default=str, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            conn.execute(
                "DELETE FROM clasificacion_contrato "
                "WHERE contrato_id = %s AND capa = 'keyword'",
                (cid,),
            )
            conn.commit()
            c = conn.execute(
                "SELECT id, categoria_it, relevancia_ia FROM contratos WHERE id = %s",
                (cid,),
            ).fetchone()
            cl = conn.execute(
                "SELECT contrato_id FROM clasificacion_contrato WHERE contrato_id = %s",
                (cid,),
            ).fetchone()
            print("deleted", cid, "contratos=", dict(c), "clasif=", cl, flush=True)
            if c["categoria_it"] is not None or c["relevancia_ia"] is not None:
                print("ERROR: eco no limpio contratos", flush=True)
                return 1
            if cl:
                print("ERROR: fila clasificacion sigue", flush=True)
                return 1
            print("OK desetiquetado", flush=True)
            return 0

        if args.cmd == "status":
            snap = json.loads(SNAP.read_text(encoding="utf-8"))
            cid = _cid(snap, args.id)
            c = conn.execute(
                "SELECT id, categoria_it, relevancia_ia FROM contratos WHERE id = %s",
                (cid,),
            ).fetchone()
            cl = conn.execute(
                "SELECT contrato_id, categoria_it, relevancia_ia, capa, artefacto "
                "FROM clasificacion_contrato WHERE contrato_id = %s",
                (cid,),
            ).fetchone()
            diff = conn.execute(
                """
                SELECT count(*)::int AS n
                FROM contratos cx
                FULL OUTER JOIN clasificacion_contrato clx
                  ON clx.contrato_id = cx.id
                WHERE (
                    cx.categoria_it IS NOT NULL OR cx.relevancia_ia IS NOT NULL
                    OR clx.contrato_id IS NOT NULL
                )
                AND (
                    cx.categoria_it IS DISTINCT FROM clx.categoria_it
                    OR cx.relevancia_ia IS DISTINCT FROM clx.relevancia_ia
                )
                """
            ).fetchone()["n"]
            print("id", cid, flush=True)
            print("contratos", dict(c) if c else None, flush=True)
            print("clasificacion", dict(cl) if cl else None, flush=True)
            print("diff", diff, flush=True)
            return 0

        if args.cmd == "restore":
            snap = json.loads(SNAP.read_text(encoding="utf-8"))
            cid = _cid(snap, args.id)
            cat = snap.get("categoria_it")
            ia = snap.get("relevancia_ia")
            kid = snap.get("keyword_id")
            art = snap.get("artefacto") or "restore_smoke"
            conn.execute(
                """
                INSERT INTO clasificacion_contrato (
                  contrato_id, categoria_it, relevancia_ia, capa,
                  keyword_id, consenso_n, artefacto
                ) VALUES (%s, %s, %s, 'keyword', %s, 0, %s)
                ON CONFLICT (contrato_id) DO UPDATE SET
                  categoria_it = EXCLUDED.categoria_it,
                  relevancia_ia = EXCLUDED.relevancia_ia,
                  capa = 'keyword',
                  keyword_id = EXCLUDED.keyword_id,
                  artefacto = EXCLUDED.artefacto,
                  actualizado_utc = now()
                WHERE clasificacion_contrato.capa = 'keyword'
                """,
                (cid, cat, ia, kid, art),
            )
            conn.commit()
            print("restored", cid, cat, ia, flush=True)
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
