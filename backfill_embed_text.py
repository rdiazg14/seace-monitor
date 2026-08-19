#!/usr/bin/env python3
"""One-shot: llena chunk_embed_text en PDF con membrete de página y resetea embedding_v2.

NO corre solo. Uso:
  uv run python backfill_embed_text.py
  uv run python backfill_embed_text.py --dry-run
  uv run python backfill_embed_text.py --limit 50

Resetea embedding_v2 SOLO en las filas que acaba de backfillear.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from supabase import create_client

from chunker_contratos import embed_text_pdf

_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

PAGE = 1_000
BATCH = 100


def paginar_pdf_con_pagina(supa, limit: int) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        take = PAGE if not limit else min(PAGE, limit - len(out))
        if take <= 0:
            break
        res = (
            supa.table("chunks_tdr")
            .select("id, contrato_id, texto, fuente")
            .eq("fuente", "pdf")
            .ilike("texto", "%--- pagina%")
            .order("id")
            .range(offset, offset + take - 1)
            .execute()
        )
        batch = res.data or []
        out.extend(batch)
        if len(batch) < take:
            break
        offset += take
        if limit and len(out) >= limit:
            break
    return out[:limit] if limit else out


def contratos_por_ids(supa, ids: list[int]) -> dict[int, dict]:
    found: dict[int, dict] = {}
    cols = (
        "id, nro_contratacion, descripcion_contrato, descripcion, entidad, objeto"
    )
    for i in range(0, len(ids), 80):
        lote = ids[i:i + 80]
        res = supa.table("contratos").select(cols).in_("id", lote).execute()
        for row in res.data or []:
            found[int(row["id"])] = row
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="0 = todos")
    args = ap.parse_args()

    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        print("ERROR: falta SUPABASE_URL / SUPABASE_SERVICE_KEY")
        return 1

    supa = create_client(url, key)
    rows = paginar_pdf_con_pagina(supa, args.limit)
    print(f"PDF con '--- pagina': {len(rows)}", flush=True)
    if not rows:
        print("Nada que backfillear.")
        return 0

    cids = sorted({int(r["contrato_id"]) for r in rows})
    contratos = contratos_por_ids(supa, cids)
    print(f"Contratos resueltos: {len(contratos)}/{len(cids)}", flush=True)

    updates: list[tuple[int, str]] = []
    skip = 0
    for r in rows:
        c = contratos.get(int(r["contrato_id"]))
        if not c:
            skip += 1
            continue
        text = embed_text_pdf(c, r.get("texto") or "")
        if not text:
            skip += 1
            continue
        updates.append((int(r["id"]), text))

    print(f"A actualizar chunk_embed_text: {len(updates)}  omitidos={skip}", flush=True)
    if args.dry_run:
        if updates:
            print(f"  dry-run ejemplo id={updates[0][0]} chars={len(updates[0][1])}")
            print(updates[0][1][:400])
        print("dry-run: no escribí ni reseteé embeddings.")
        return 0

    n_txt = 0
    for i in range(0, len(updates), BATCH):
        lote = updates[i:i + BATCH]
        for cid, text in lote:
            supa.table("chunks_tdr").update({"chunk_embed_text": text}).eq("id", cid).execute()
            n_txt += 1
        print(f"  embed_text {min(i + BATCH, len(updates))}/{len(updates)}", flush=True)

    ids = [cid for cid, _ in updates]
    n_reset = 0
    for i in range(0, len(ids), BATCH):
        lote = ids[i:i + BATCH]
        res = (
            supa.table("chunks_tdr")
            .update({"embedding_v2": None})
            .in_("id", lote)
            .eq("fuente", "pdf")
            .execute()
        )
        n_reset += len(res.data or [])

    print(f"N chunks actualizados (chunk_embed_text): {n_txt}")
    print(f"N embeddings reseteados (embedding_v2=NULL): {n_reset}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
