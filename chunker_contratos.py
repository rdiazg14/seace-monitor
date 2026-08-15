#!/usr/bin/env python3
"""
Fase 3 — Chunking de TDR (sin PDFs).

Texto RAG por contrato:
  a) descripcion  ← desObjetoContrato (TDR general)
  b) items_json[] ← specs técnicas CUBSO
  c) metadata     ← entidad, área, objeto, estado, número

Schema real de chunks_tdr (Fase 1):
  contrato_id, chunk_index, tipo, texto, embedding
  (tipo = sección: "Descripción general" | "Ítem técnico N" | "Metadata")

Uso: python chunker_contratos.py [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from supabase import create_client

_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

PAGE = 1_000
BATCH_INSERT = 200
MAX_TOKENS_ANTES_SPLIT = 800
TARGET_SUBCHUNK = 500


def approx_tokens(texto: str) -> int:
    """Estimación barata (~4 chars/token). Evita dependencia de tiktoken."""
    if not texto:
        return 0
    return max(1, len(texto) // 4)


def split_por_parrafos(texto: str, target_tokens: int) -> list[str]:
    partes = [p.strip() for p in texto.replace("\r\n", "\n").split("\n") if p.strip()]
    if not partes:
        return [texto.strip()] if texto.strip() else []

    chunks: list[str] = []
    buf: list[str] = []
    buf_tok = 0
    for p in partes:
        pt = approx_tokens(p)
        if buf and buf_tok + pt > target_tokens:
            chunks.append("\n".join(buf))
            buf, buf_tok = [p], pt
        else:
            buf.append(p)
            buf_tok += pt
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def chunks_de_contrato(c: dict) -> list[dict]:
    cid = c["id"]
    out: list[dict] = []
    idx = 0

    tdr = (c.get("descripcion") or "").strip()
    if tdr:
        partes = (
            split_por_parrafos(tdr, TARGET_SUBCHUNK)
            if approx_tokens(tdr) > MAX_TOKENS_ANTES_SPLIT
            else [tdr]
        )
        n = len(partes)
        for i, parte in enumerate(partes, 1):
            tipo = "Descripción general" if n == 1 else f"Descripción general ({i}/{n})"
            out.append({
                "contrato_id": cid,
                "chunk_index": idx,
                "tipo": tipo,
                "texto": parte,
            })
            idx += 1

    items = c.get("items_json") or []
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except json.JSONDecodeError:
            items = []
    if not isinstance(items, list):
        items = []

    for n, it in enumerate(items, 1):
        if not isinstance(it, dict):
            continue
        texto = (
            f"CUBSO: {it.get('cod_cubso') or ''} - {it.get('nom_cubso') or ''}. "
            f"Cantidad: {it.get('cantidad') or ''} {it.get('unidad') or ''}. "
            f"Lugar: {it.get('distrito') or ''}. "
            f"Especificaciones: {it.get('descripcion') or ''}"
        ).strip()
        out.append({
            "contrato_id": cid,
            "chunk_index": idx,
            "tipo": f"Ítem técnico {n}",
            "texto": texto,
        })
        idx += 1

    meta = (
        f"Entidad: {c.get('entidad') or ''}. "
        f"Área usuaria: {c.get('nom_area_usuaria') or ''}. "
        f"Objeto: {c.get('objeto') or ''}. "
        f"Estado: {c.get('estado') or ''}. "
        f"Número: {c.get('nro_contratacion') or ''}."
    )
    out.append({
        "contrato_id": cid,
        "chunk_index": idx,
        "tipo": "Metadata",
        "texto": meta,
    })
    return out


def paginar(supa, tabla: str, cols: str, **filters) -> list[dict]:
    out: list[dict] = []
    offset = 0
    qbase = supa.table(tabla).select(cols)
    for k, v in filters.items():
        if k == "eq":
            for col, val in v.items():
                qbase = qbase.eq(col, val)
    while True:
        q = supa.table(tabla).select(cols)
        for k, v in filters.items():
            if k == "eq":
                for col, val in v.items():
                    q = q.eq(col, val)
        res = q.range(offset, offset + PAGE - 1).execute()
        batch = res.data or []
        out.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return out


def ids_ya_chunkeados(supa) -> set[int]:
    ids: set[int] = set()
    offset = 0
    while True:
        res = (
            supa.table("chunks_tdr")
            .select("contrato_id")
            .range(offset, offset + PAGE - 1)
            .execute()
        )
        batch = res.data or []
        for row in batch:
            ids.add(int(row["contrato_id"]))
        if len(batch) < PAGE:
            break
        offset += PAGE
    return ids


def insert_lote(supa, lote: list[dict]):
    supa.table("chunks_tdr").upsert(
        lote, on_conflict="contrato_id,chunk_index"
    ).execute()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = todos")
    args = ap.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SystemExit("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY no encontrados")

    supa = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("=" * 60, flush=True)
    print("FASE 3 — Chunking de TDR", flush=True)
    print("=" * 60, flush=True)

    print("Cargando contratos con detalle...", flush=True)
    contratos = paginar(
        supa,
        "contratos",
        "id, nro_contratacion, descripcion, entidad, objeto, estado, nom_area_usuaria, items_json",
        eq={"detalle_cargado": True},
    )
    ya = ids_ya_chunkeados(supa)
    pendientes = [c for c in contratos if int(c["id"]) not in ya]
    if args.limit:
        pendientes = pendientes[: args.limit]

    print(f"  Con detalle     : {len(contratos):,}", flush=True)
    print(f"  Ya con chunks   : {len(ya):,}", flush=True)
    print(f"  Pendientes      : {len(pendientes):,}", flush=True)
    if not pendientes:
        print("Nada que hacer.", flush=True)
        return

    t0 = time.time()
    buffer: list[dict] = []
    n_chunks = 0
    n_contratos = 0
    dist: dict[str, int] = {}
    token_sum = 0

    for i, c in enumerate(pendientes, 1):
        try:
            chs = chunks_de_contrato(c)
        except Exception as e:
            print(f"  [error] contrato {c.get('id')}: {e}", flush=True)
            continue
        for ch in chs:
            tok = approx_tokens(ch["texto"])
            token_sum += tok
            tipo_base = ch["tipo"].split(" (")[0]
            if tipo_base.startswith("Ítem técnico"):
                tipo_base = "Ítem técnico"
            dist[tipo_base] = dist.get(tipo_base, 0) + 1
        buffer.extend(chs)
        n_chunks += len(chs)
        n_contratos += 1

        if len(buffer) >= BATCH_INSERT:
            try:
                insert_lote(supa, buffer)
                print(f"  [{i}/{len(pendientes)}] insert {len(buffer)} chunks "
                      f"(acum {n_chunks:,})", flush=True)
            except Exception as e:
                print(f"  [error] upsert lote: {e}", flush=True)
            buffer = []

    if buffer:
        try:
            insert_lote(supa, buffer)
            print(f"  insert lote final {len(buffer)} chunks", flush=True)
        except Exception as e:
            print(f"  [error] upsert lote final: {e}", flush=True)

    elapsed = time.time() - t0
    print(f"\n{'='*60}", flush=True)
    print(f"Fase 3 completada en {elapsed:.0f}s", flush=True)
    print(f"  Contratos procesados : {n_contratos:,}", flush=True)
    print(f"  Chunks generados     : {n_chunks:,}", flush=True)
    if n_contratos:
        print(f"  Promedio chunks/contrato : {n_chunks / n_contratos:.2f}", flush=True)
    if n_chunks:
        print(f"  Promedio tokens/chunk    : {token_sum / n_chunks:.0f}", flush=True)
    print("  Distribución por sección:", flush=True)
    for k, v in sorted(dist.items(), key=lambda kv: -kv[1]):
        print(f"    {k}: {v:,}", flush=True)

    ej = (
        supa.table("chunks_tdr")
        .select("contrato_id, chunk_index, tipo, texto")
        .order("id", desc=True)
        .limit(3)
        .execute()
    )
    print("\n--- 3 ejemplos de chunks ---", flush=True)
    for row in ej.data or []:
        texto = row.get("texto") or ""
        print(json.dumps({
            "contrato_id": row["contrato_id"],
            "chunk_index": row["chunk_index"],
            "tipo": row["tipo"],
            "n_tokens_aprox": approx_tokens(texto),
            "texto": texto[:400],
        }, ensure_ascii=False, indent=2), flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    main()
