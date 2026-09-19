#!/usr/bin/env python3
"""Reparación: resetear analizado=false en contratos con flag sin payload.

El flag `contratos.analizado=true` sin fila en `analisis_contrato` es un falso
positivo del funnel (marca KV escrita aunque el payload no se persistió). Fix
de causa raíz ya aplicado en el Worker (marcarFunnel condicionado a
escribirAnalisisBd). Aquí se repara el dato histórico:

  1. Re-identifica los analizado=true sin payload (guard NOT EXISTS).
  2. Hace UPDATE ... SET analizado=false SOLO en esos (nunca pisa un payload
     real; si entremedio se persistió un payload, el guard lo excluye).
  3. Reporta los ids para borrar la marca KV (funnel:analizado:{id}) aparte.

Idempotente: re-correr es no-op. No toca cotizado ni fechas.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

_env = Path(__file__).resolve().parent.parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

DRY = "--dry-run" in sys.argv

with psycopg.connect(os.environ["DATABASE_URL"], autocommit=False) as conn:
    conn.execute("SET statement_timeout='60s'")

    # 1. Identificar los inconsistentes (flag sin payload).
    rows = conn.execute(
        "SELECT c.id FROM contratos c "
        "WHERE c.analizado = true "
        "  AND NOT EXISTS (SELECT 1 FROM analisis_contrato a WHERE a.contrato_id = c.id) "
        "ORDER BY c.id"
    ).fetchall()
    ids = [r[0] for r in rows]
    print(f"analizado=true sin payload: {len(ids)}", flush=True)
    print("ids:", " ".join(str(i) for i in ids), flush=True)

    if not ids:
        print("nada que reparar", flush=True)
        raise SystemExit(0)

    if DRY:
        print("[dry-run] sin escribir", flush=True)
        conn.rollback()
        raise SystemExit(0)

    # 2. Resetear el flag SOLO si sigue sin payload (guard atómico por fila).
    cur = conn.cursor()
    updated = 0
    for cid in ids:
        cur.execute(
            "UPDATE contratos c SET analizado = false "
            "WHERE c.id = %s AND c.analizado = true "
            "  AND NOT EXISTS (SELECT 1 FROM analisis_contrato a WHERE a.contrato_id = c.id)",
            (cid,),
        )
        updated += cur.rowcount
    conn.commit()
    print(f"reseteados: {updated}/{len(ids)}", flush=True)

    # 3. Verificación final.
    left = conn.execute(
        "SELECT count(*) FROM contratos c "
        "WHERE c.analizado = true "
        "  AND NOT EXISTS (SELECT 1 FROM analisis_contrato a WHERE a.contrato_id = c.id)"
    ).fetchone()[0]
    print(f"quedan inconsistentes: {left}", flush=True)

    print("\nAhora borra las marcas KV (funnel:analizado:{id}) para estos ids:", flush=True)
    for i in ids:
        print(f"  npx wrangler kv key delete --namespace-id c63cfd497041477f91dafdde5935f37d --remote \"funnel:analizado:{i}\"", flush=True)
