#!/usr/bin/env python3
"""
Chunking de TDR.

fuente=api: encabezado largo [ENTIDAD | asunto | Nº] (corpus general, no se toca).
fuente=pdf: header corto [SIGLAS | Nº] en `texto` (display LLM) + meta_entidad/meta_nro.
            El embed v2 usa el cuerpo sin header (--embed-mode auto/body).

Uso:
  python chunker_contratos.py [--limit N]
  python chunker_contratos.py --rechunk
  python chunker_contratos.py --solo-pdf --ids 87164,87001
  python chunker_contratos.py --solo-pdf --solo-nuevos
"""
from __future__ import annotations

import argparse
import json
import os
import re
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


_STOP_SIGLAS = frozenset({
    "DE", "DEL", "Y", "E", "DA", "DO", "DAS", "AL", "A",
})
HEADER_LINE_RE = re.compile(r"^\[[^\]]+\]\s*(?:\n|$)")


def nro_contrato(c: dict) -> str:
    return (
        (c.get("descripcion_contrato") or "").strip()
        or str(c.get("nro_contratacion") or "")
        or str(c.get("id") or "")
    )


def siglas_entidad(entidad: str) -> str:
    """Iniciales del tramo más específico (después del último guion)."""
    raw = (entidad or "").strip()
    if not raw:
        return "s/e"
    partes = [p.strip() for p in re.split(r"\s*[-–—/]\s*", raw) if p.strip()]
    foco = partes[-1] if partes else raw
    iniciales: list[str] = []
    for w in re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9]+", foco):
        u = w.upper()
        if u in _STOP_SIGLAS:
            continue
        iniciales.append(u[0])
    sig = "".join(iniciales)
    if len(sig) < 2:
        sig = re.sub(r"[^A-Za-z0-9]", "", foco)[:12].upper() or "s/e"
    return sig[:16]


def encabezado(c: dict) -> str:
    """Header largo de chunks fuente=api (no se cambia el corpus general)."""
    entidad = (c.get("entidad") or "").strip() or "s/e"
    asunto = (c.get("descripcion") or c.get("objeto") or "").strip()
    asunto = " ".join(asunto.split())[:80] or "s/a"
    return f"[{entidad} | {asunto} | {nro_contrato(c)}]"


def encabezado_pdf(c: dict) -> str:
    """Header corto para display: siglas + Nº. Sin asunto (ya está en metadata api)."""
    return f"[{siglas_entidad(c.get('entidad') or '')} | {nro_contrato(c)}]"


def cuerpo_chunk(texto: str) -> str:
    """Cuerpo sin la primera línea [header]. Eso es lo que se embebe en modo body."""
    t = texto or ""
    m = HEADER_LINE_RE.match(t)
    return t[m.end():].lstrip() if m else t


def con_contexto(c: dict, texto: str) -> str:
    return f"{encabezado(c)}\n{texto}"


def con_contexto_pdf(c: dict, texto: str) -> str:
    return f"{encabezado_pdf(c)}\n{texto}"


def meta_de_contrato(c: dict) -> dict:
    nro = nro_contrato(c)
    return {
        "meta_entidad": (c.get("entidad") or "").strip() or None,
        "meta_nro": nro or None,
    }


def chunks_de_pdf(c: dict, chunk_index_offset: int = 0) -> list[dict]:
    """Chunks del TDR extraído (tdr_texto). chunk_index sigue a los de fuente=api."""
    tdr = (c.get("tdr_texto") or "").strip()
    if not tdr:
        return []
    cid = c["id"]
    partes = (
        split_por_parrafos(tdr, TARGET_SUBCHUNK)
        if approx_tokens(tdr) > MAX_TOKENS_ANTES_SPLIT
        else [tdr]
    )
    meta = meta_de_contrato(c)
    out: list[dict] = []
    for i, parte in enumerate(partes):
        row = {
            "contrato_id": cid,
            "chunk_index": chunk_index_offset + i,
            "tipo": "TDR PDF" if len(partes) == 1 else f"TDR PDF ({i + 1}/{len(partes)})",
            "texto": con_contexto_pdf(c, parte),
            "fuente": "pdf",
        }
        row.update(meta)
        out.append(row)
    return out


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
                "texto": con_contexto(c, parte),
                "fuente": "api",
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
            "texto": con_contexto(c, texto),
            "fuente": "api",
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
        "texto": con_contexto(c, meta),
        "fuente": "api",
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


def cobertura_fuentes(supa) -> dict:
    """Cuántos vigentes tienen chunks api y/o pdf conviviendo."""
    vigentes = paginar(
        supa, "contratos", "id,tdr_texto", eq={"estado": "Vigente"},
    )
    con_tdr = {int(c["id"]) for c in vigentes if (c.get("tdr_texto") or "").strip()}
    ids = [int(c["id"]) for c in vigentes]
    api_ids: set[int] = set()
    pdf_ids: set[int] = set()
    n_pdf = n_api = 0
    n_pdf_v2 = n_api_v2 = 0
    for i in range(0, len(ids), 80):
        lote = ids[i:i + 80]
        offset = 0
        while True:
            res = (
                supa.table("chunks_tdr")
                .select("contrato_id,fuente")
                .in_("contrato_id", lote)
                .range(offset, offset + PAGE - 1)
                .execute()
            )
            batch = res.data or []
            for row in batch:
                cid = int(row["contrato_id"])
                if (row.get("fuente") or "api") == "pdf":
                    pdf_ids.add(cid)
                    n_pdf += 1
                else:
                    api_ids.add(cid)
                    n_api += 1
            if len(batch) < PAGE:
                break
            offset += PAGE
        pdf_v2 = (
            supa.table("chunks_tdr")
            .select("id", count="exact", head=True)
            .in_("contrato_id", lote)
            .eq("fuente", "pdf")
            .not_.is_("embedding_v2", "null")
            .execute()
        )
        api_v2 = (
            supa.table("chunks_tdr")
            .select("id", count="exact", head=True)
            .in_("contrato_id", lote)
            .eq("fuente", "api")
            .not_.is_("embedding_v2", "null")
            .execute()
        )
        n_pdf_v2 += pdf_v2.count or 0
        n_api_v2 += api_v2.count or 0
    return {
        "vigentes": len(vigentes),
        "con_tdr_texto": len(con_tdr),
        "contratos_chunk_api": len(api_ids),
        "contratos_chunk_pdf": len(pdf_ids),
        "contratos_api_y_pdf": len(api_ids & pdf_ids),
        "tdr_sin_chunk_pdf": len(con_tdr - pdf_ids),
        "chunks_api": n_api,
        "chunks_pdf": n_pdf,
        "chunks_api_v2": n_api_v2,
        "chunks_pdf_v2": n_pdf_v2,
    }


def print_cobertura_fuentes(cov: dict) -> None:
    print("\n--- cobertura api + pdf ---", flush=True)
    for k, v in cov.items():
        print(f"  {k}={v}", flush=True)
    n = cov.get("vigentes") or 0
    ambos = cov.get("contratos_api_y_pdf") or 0
    if n:
        print(
            f"  vigentes con ambas fuentes: {ambos}/{n} "
            f"({100.0 * ambos / n:.1f}%)",
            flush=True,
        )


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
    try:
        supa.table("chunks_tdr").upsert(
            lote, on_conflict="contrato_id,chunk_index"
        ).execute()
    except Exception as e:
        msg = str(e).lower()
        if "meta_entidad" in msg or "meta_nro" in msg or "pgrst204" in msg:
            print(
                "  [aviso] columnas meta_entidad/meta_nro ausentes; "
                "aplica chunks_pdf_meta.sql. Inserto sin metadata.",
                flush=True,
            )
            stripped = [
                {k: v for k, v in row.items() if k not in ("meta_entidad", "meta_nro")}
                for row in lote
            ]
            supa.table("chunks_tdr").upsert(
                stripped, on_conflict="contrato_id,chunk_index"
            ).execute()
        else:
            raise


def ids_con_fuente_pdf(supa, ids: list[int]) -> set[int]:
    """Contratos de `ids` que ya tienen al menos un chunk fuente=pdf."""
    found: set[int] = set()
    if not ids:
        return found
    for i in range(0, len(ids), 80):
        lote = [int(x) for x in ids[i:i + 80]]
        offset = 0
        lote_found: set[int] = set()
        while True:
            res = (
                supa.table("chunks_tdr")
                .select("contrato_id")
                .in_("contrato_id", lote)
                .eq("fuente", "pdf")
                .range(offset, offset + PAGE - 1)
                .execute()
            )
            batch = res.data or []
            for row in batch:
                lote_found.add(int(row["contrato_id"]))
            if len(batch) < PAGE or len(lote_found) >= len(lote):
                break
            offset += PAGE
        found |= lote_found
    return found


def borrar_chunks_fuente(supa, ids: list[int], fuente: str) -> None:
    """Borra solo chunks de una fuente. No toca las demás."""
    for i in range(0, len(ids), 80):
        lote = ids[i:i + 80]
        (
            supa.table("chunks_tdr")
            .delete()
            .in_("contrato_id", lote)
            .eq("fuente", fuente)
            .execute()
        )


def max_chunk_index(supa, contrato_id: int) -> int:
    res = (
        supa.table("chunks_tdr")
        .select("chunk_index")
        .eq("contrato_id", contrato_id)
        .order("chunk_index", desc=True)
        .limit(1)
        .execute()
    )
    if not res.data:
        return -1
    return int(res.data[0]["chunk_index"])


def borrar_chunks_vigentes(supa, ids: list[int]):
    for i in range(0, len(ids), 80):
        lote = ids[i:i + 80]
        supa.table("chunks_tdr").delete().in_("contrato_id", lote).execute()


def run_solo_pdf(
    supa, ids_fijos: list[int], limit: int, *, solo_nuevos: bool = False
) -> None:
    """Inserta chunks fuente=pdf. No borra ni reescribe fuente=api."""
    cols = (
        "id, nro_contratacion, descripcion_contrato, descripcion, entidad, "
        "objeto, estado, nom_area_usuaria, items_json, tdr_texto"
    )
    if ids_fijos:
        res = supa.table("contratos").select(cols).in_("id", ids_fijos).execute()
        by_id = {int(r["id"]): r for r in (res.data or [])}
        contratos = [by_id[i] for i in ids_fijos if i in by_id]
    else:
        contratos = paginar(
            supa, "contratos", cols,
            eq={"estado": "Vigente"},
        )
    pendientes = [c for c in contratos if (c.get("tdr_texto") or "").strip()]
    if limit:
        pendientes = pendientes[:limit]
    print(f"  contratos con tdr_texto: {len(pendientes)}", flush=True)
    if solo_nuevos and not ids_fijos:
        ids_all = [int(c["id"]) for c in pendientes]
        ya_pdf = ids_con_fuente_pdf(supa, ids_all)
        n_skip = sum(1 for c in pendientes if int(c["id"]) in ya_pdf)
        pendientes = [c for c in pendientes if int(c["id"]) not in ya_pdf]
        print(
            f"  --solo-nuevos: omitidos con pdf chunks={n_skip} "
            f"quedan={len(pendientes)}",
            flush=True,
        )
    elif solo_nuevos and ids_fijos:
        print(
            "  --solo-nuevos + --ids: se reescriben esos ids",
            flush=True,
        )
    if not pendientes:
        print("Nada que hacer (sin tdr_texto / ya chunkeados pdf).", flush=True)
        return

    ids = [int(c["id"]) for c in pendientes]
    borrar_chunks_fuente(supa, ids, "pdf")
    print("  chunks fuente=pdf previos de la muestra borrados", flush=True)

    t0 = time.time()
    buffer: list[dict] = []
    n_chunks = 0
    n_contratos = 0
    for i, c in enumerate(pendientes, 1):
        try:
            offset = max_chunk_index(supa, int(c["id"])) + 1
            chs = chunks_de_pdf(c, chunk_index_offset=offset)
        except Exception as e:
            print(f"  [error] contrato {c.get('id')}: {e}", flush=True)
            continue
        if not chs:
            print(f"  [{i}] id={c.get('id')} sin chunks pdf", flush=True)
            continue
        buffer.extend(chs)
        n_chunks += len(chs)
        n_contratos += 1
        if len(pendientes) <= 40 or i % 25 == 0 or i == len(pendientes):
            print(
                f"  [{i}/{len(pendientes)}] id={c['id']} "
                f"pdf_chunks={len(chs)} offset={offset} "
                f"header={encabezado_pdf(c)}",
                flush=True,
            )
        if len(buffer) >= BATCH_INSERT:
            insert_lote(supa, buffer)
            buffer = []
    if buffer:
        insert_lote(supa, buffer)

    elapsed = time.time() - t0
    print(f"\n{'='*60}", flush=True)
    print(f"PDF chunking en {elapsed:.0f}s  contratos={n_contratos} chunks={n_chunks}", flush=True)
    if ids_fijos or solo_nuevos:
        print("=" * 60, flush=True)
        return
    print_cobertura_fuentes(cobertura_fuentes(supa))
    if pendientes:
        cid = int(pendientes[0]["id"])
        ej = (
            supa.table("chunks_tdr")
            .select("contrato_id, chunk_index, tipo, texto, fuente")
            .eq("contrato_id", cid)
            .eq("fuente", "pdf")
            .order("chunk_index")
            .limit(3)
            .execute()
        )
        print(f"--- ejemplos fuente=pdf contrato {cid} ---", flush=True)
        for row in ej.data or []:
            texto = row.get("texto") or ""
            print(json.dumps({
                "contrato_id": row["contrato_id"],
                "chunk_index": row["chunk_index"],
                "tipo": row["tipo"],
                "fuente": row.get("fuente"),
                "n_tokens_aprox": approx_tokens(texto),
                "cuerpo": cuerpo_chunk(texto)[:240],
                "texto": texto[:400],
            }, ensure_ascii=False, indent=2), flush=True)
    print("=" * 60, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = todos")
    ap.add_argument("--ids", default="",
                    help="Ids fijos separados por coma")
    ap.add_argument("--rechunk", action="store_true",
                    help="Borra/reinserta chunks de vigentes (no toca cerrados ni En Evaluacion)")
    ap.add_argument("--solo-pdf", action="store_true",
                    help="Solo chunks fuente=pdf de contratos con tdr_texto. No toca fuente=api.")
    ap.add_argument(
        "--solo-nuevos",
        action="store_true",
        help="Con --solo-pdf: no reescribe contratos que ya tienen chunks fuente=pdf",
    )
    args = ap.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SystemExit("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY no encontrados")

    ids_fijos = []
    if args.ids:
        ids_fijos = [int(x) for x in args.ids.replace(" ", "").split(",") if x]

    supa = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("=" * 60, flush=True)
    print(
        f"Chunking TDR  (solo-pdf={args.solo_pdf} solo-nuevos={args.solo_nuevos} "
        f"rechunk={args.rechunk} ids={ids_fijos or '-'})",
        flush=True,
    )
    print("=" * 60, flush=True)

    if args.solo_nuevos and not args.solo_pdf:
        raise SystemExit("--solo-nuevos solo aplica con --solo-pdf")

    if args.solo_pdf:
        run_solo_pdf(supa, ids_fijos, args.limit, solo_nuevos=args.solo_nuevos)
        return

    print("Cargando vigentes con detalle...", flush=True)
    contratos = paginar(
        supa,
        "contratos",
        "id, nro_contratacion, descripcion_contrato, descripcion, entidad, objeto, estado, nom_area_usuaria, items_json, tdr_texto",
        eq={"detalle_cargado": True, "estado": "Vigente"},
    )
    if args.rechunk:
        pendientes = contratos
        print(f"  --rechunk: vigentes con detalle = {len(pendientes):,}", flush=True)
    else:
        ya = ids_ya_chunkeados(supa)
        pendientes = [c for c in contratos if int(c["id"]) not in ya]
        print(f"  Con detalle vigente : {len(contratos):,}", flush=True)
        print(f"  Ya con chunks       : {len(ya):,}", flush=True)
        print(f"  Pendientes          : {len(pendientes):,}", flush=True)
    if args.limit:
        pendientes = pendientes[: args.limit]
        print(f"  --limit {args.limit}: se procesan {len(pendientes):,}", flush=True)

    if not pendientes:
        print("Nada que hacer.", flush=True)
        return

    if args.rechunk:
        borrar_chunks_vigentes(supa, [int(c["id"]) for c in pendientes])
        print("  chunks previos de esos vigentes borrados", flush=True)

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
        .select("contrato_id, chunk_index, tipo, texto, fuente")
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
