#!/usr/bin/env python3
"""Diagnóstico RAG / #10 para un contrato (chunks, retrieve v2, texto de /analizar).

Uso:
  uv run python diag_contrato_rag.py
  uv run python diag_contrato_rag.py --nro "CM-20-2026-CENEPRED/OA/UFL"
  uv run python diag_contrato_rag.py --id 12345
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx
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
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_EMBED_MODEL = "gemini-embedding-001"
GEMINI_DIM = 1536
MIN_SIM = 0.20
MATCH_COUNT = 20
TOP_SHOW = 5
TDR_MIN_CHARS = 200
TDR_MAX_CHARS = 60_000
NRO_DEFAULT = "CM-20-2026-CENEPRED/OA/UFL"

QUERIES = [
    "requisitos proveedor certificados experiencia",
    "entregables plazo sílabo conformidad",
    "confidencialidad plataforma materiales propiedad",
]


def l2_normalize(vec: list[float]) -> list[float]:
    s = sum(x * x for x in vec) ** 0.5
    if s <= 0:
        return vec
    return [x / s for x in vec]


def embed_query(client: httpx.Client, text: str) -> list[float]:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_EMBED_MODEL}:embedContent"
    )
    r = client.post(
        url,
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
        json={
            "model": f"models/{GEMINI_EMBED_MODEL}",
            "content": {"parts": [{"text": text[:8000]}]},
            "taskType": "RETRIEVAL_QUERY",
            "outputDimensionality": GEMINI_DIM,
        },
        timeout=60.0,
    )
    r.raise_for_status()
    vals = (r.json().get("embedding") or {}).get("values") or []
    if len(vals) > GEMINI_DIM:
        vals = vals[:GEMINI_DIM]
    if len(vals) != GEMINI_DIM:
        raise RuntimeError(f"gemini query dim {len(vals)} != {GEMINI_DIM}")
    return l2_normalize([float(x) for x in vals])


def parse_vec(ev) -> list[float] | None:
    if ev is None:
        return None
    if isinstance(ev, (list, tuple)):
        try:
            return [float(x) for x in ev]
        except (TypeError, ValueError):
            return None
    if isinstance(ev, str):
        s = ev.strip()
        if s.startswith("[") and s.endswith("]"):
            s = s[1:-1]
        elif s.startswith("{") and s.endswith("}"):
            s = s[1:-1]
        if not s:
            return None
        try:
            return [float(x) for x in s.split(",") if x.strip()]
        except ValueError:
            return None
    return None


def cosine(a: list[float], b: list[float]) -> float:
    return float(sum(x * y for x, y in zip(a, b)))


def sep(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72, flush=True)


def clip(s: str, n: int) -> str:
    t = (s or "").replace("\r\n", "\n")
    if len(t) <= n:
        return t
    return t[:n] + "…"


def buscar_contrato(supa, nro: str | None, cid: int | None) -> dict:
    if cid:
        r = (
            supa.table("contratos")
            .select(
                "id,nro_contratacion,descripcion_contrato,descripcion,entidad,estado,"
                "objeto,nom_area_usuaria,fecha_publica,fecha_fin_cotizacion,"
                "tipo_cotizacion,categoria_it,relevancia_ia,tdr_texto,pdf_hash,req_url"
            )
            .eq("id", cid)
            .limit(1)
            .execute()
        )
        if r.data:
            return r.data[0]
        raise SystemExit(f"No hay contrato id={cid}")

    needle = (nro or NRO_DEFAULT).strip()
    r = (
        supa.table("contratos")
        .select(
            "id,nro_contratacion,descripcion_contrato,descripcion,entidad,estado,"
            "objeto,nom_area_usuaria,fecha_publica,fecha_fin_cotizacion,"
            "tipo_cotizacion,categoria_it,relevancia_ia,tdr_texto,pdf_hash,req_url"
        )
        .or_(
            f"descripcion_contrato.ilike.%{needle}%,"
            f"nro_contratacion.ilike.%{needle}%"
        )
        .limit(10)
        .execute()
    )
    rows = r.data or []
    if not rows:
        raise SystemExit(f"No encontré contrato con nro/descripcion ~ {needle!r}")
    if len(rows) > 1:
        print(f"AVISO: {len(rows)} filas; uso la primera.", flush=True)
        for x in rows:
            print(
                f"  id={x['id']} nro={x.get('nro_contratacion')} "
                f"desc={x.get('descripcion_contrato')}",
                flush=True,
            )
    return rows[0]


def fetch_chunks(supa, cid: int) -> list[dict]:
    out: list[dict] = []
    offset = 0
    page = 200
    while True:
        r = (
            supa.table("chunks_tdr")
            .select("chunk_index,tipo,fuente,texto,embedding_v2")
            .eq("contrato_id", cid)
            .order("chunk_index")
            .range(offset, offset + page - 1)
            .execute()
        )
        batch = r.data or []
        out.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return out


def armar_tdr_analizar(ficha: dict, chunks: list[dict]) -> tuple[str, str, str]:
    """Réplica de handleAnalizar (analizar.ts L273–320, L326–345)."""
    tdr_col = str(ficha.get("tdr_texto") or "").strip()
    if tdr_col:
        tdr = tdr_col
        fuente = "tdr_texto"
        nota = "columna contratos.tdr_texto (los chunks NO entran al prompt)"
    else:
        top = chunks[:80]
        if top:
            tdr = "\n\n".join(
                f"[{c.get('fuente') or 'api'} · {c.get('tipo') or 'chunk'}]\n"
                f"{c.get('texto') or ''}"
                for c in top
            )
            fuente = "chunks"
            nota = f"concat de chunks_tdr (limit 80, order chunk_index) — {len(top)} chunks"
        else:
            tdr = ""
            fuente = "ficha"
            nota = "sin tdr_texto y sin chunks; handleAnalizar usaria ficha vacia -> 422 si <200"

    trunc = False
    if len(tdr) > TDR_MAX_CHARS:
        tdr = tdr[:TDR_MAX_CHARS]
        trunc = True
        nota += f" · truncado a {TDR_MAX_CHARS}"
    return tdr, fuente, nota if not trunc else nota


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--nro", default=NRO_DEFAULT)
    ap.add_argument("--id", type=int, default=0)
    args = ap.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: .env sin SUPABASE_URL / SUPABASE_SERVICE_KEY", file=sys.stderr)
        return 1

    supa = create_client(SUPABASE_URL, SUPABASE_KEY)
    ficha = buscar_contrato(supa, args.nro if not args.id else None, args.id or None)
    cid = int(ficha["id"])
    estado = str(ficha.get("estado") or "")
    nro = ficha.get("descripcion_contrato") or ficha.get("nro_contratacion") or cid

    print(f"Contrato id={cid}", flush=True)
    print(f"  nro={nro}", flush=True)
    print(f"  entidad={ficha.get('entidad')}", flush=True)
    print(f"  estado={estado}", flush=True)
    print(f"  objeto={ficha.get('objeto')}", flush=True)
    print(f"  req_url={ficha.get('req_url')}", flush=True)
    tdr_len = len(str(ficha.get("tdr_texto") or "").strip())
    print(f"  tdr_texto chars={tdr_len}  pdf_hash={ficha.get('pdf_hash') or '—'}", flush=True)

    chunks = fetch_chunks(supa, cid)

    sep("1. CHUNKS EN BD")
    print(f"Total chunks_tdr: {len(chunks)}", flush=True)
    n_v2 = 0
    for c in chunks:
        has = c.get("embedding_v2") is not None
        if has:
            n_v2 += 1
        texto = c.get("texto") or ""
        print(
            f"\n  [{c.get('chunk_index')}] fuente={c.get('fuente') or '—'}  "
            f"tipo={c.get('tipo') or '—'}  chars={len(texto)}  "
            f"embedding_v2={'sí' if has else 'NO'}",
            flush=True,
        )
        print(f"      {clip(texto, 200)!r}", flush=True)
    print(f"\nCon embedding_v2: {n_v2}/{len(chunks)}", flush=True)

    sep("2. SIMULATE RAG v2 (vector buscar_tdr_v2, pre-RRF/rerank)")
    print(
        "Replica Worker: gemini-embedding-001 RETRIEVAL_QUERY @1536 L2 -> "
        f"buscar_tdr_v2 match_count={MATCH_COUNT} min_similarity={MIN_SIM}.",
        flush=True,
    )
    print(
        f"filter_estado={estado!r} (estado de ESTE contrato; el chat default es 'Vigente' "
        "si el usuario no pide otro).",
        flush=True,
    )
    if not GEMINI_API_KEY:
        print("ERROR: falta GEMINI_API_KEY en .env — no puedo embeber queries.", flush=True)
        return 1

    with httpx.Client() as http:
        for q in QUERIES:
            print(f"\n--- query: {q!r} ---", flush=True)
            vec = embed_query(http, q)
            res = supa.rpc(
                "buscar_tdr_v2",
                {
                    "query_embedding": vec,
                    "match_count": MATCH_COUNT,
                    "filter_estado": estado or "Vigente",
                    "min_similarity": MIN_SIM,
                },
            ).execute()
            hits = res.data or []
            propios = [h for h in hits if int(h.get("contrato_id") or 0) == cid]
            print(
                f"  hits globales={len(hits)}  de este contrato en el top {MATCH_COUNT}: {len(propios)}",
                flush=True,
            )
            if not hits:
                print("  (0 hits sobre el umbral 0.20)", flush=True)
            for i, h in enumerate(hits[:TOP_SHOW], 1):
                hid = int(h.get("contrato_id") or 0)
                mark = "  << ESTE CONTRATO" if hid == cid else ""
                sim = h.get("similarity")
                sim_s = f"{float(sim):.4f}" if sim is not None else "—"
                print(
                    f"  #{i} cid={hid} idx={h.get('chunk_index')} "
                    f"fuente={h.get('fuente')} tipo={h.get('tipo')} "
                    f"sim={sim_s}{mark}",
                    flush=True,
                )
                print(f"      {clip(h.get('texto') or '', 300)!r}", flush=True)

            if n_v2:
                print("  cosine local (chunks de ESTE contrato vs query):", flush=True)
                scored: list[tuple[float, dict]] = []
                for c in chunks:
                    ev = parse_vec(c.get("embedding_v2"))
                    if not ev:
                        continue
                    scored.append((cosine(vec, ev), c))
                scored.sort(key=lambda x: x[0], reverse=True)
                for sim, c in scored[:5]:
                    print(
                        f"    idx={c.get('chunk_index')} fuente={c.get('fuente')} "
                        f"cos={sim:.4f}  {clip(c.get('texto') or '', 120)!r}",
                        flush=True,
                    )

    sep("3. TEXTO QUE VIO EL LLM EN /analizar")
    tdr, fuente, nota = armar_tdr_analizar(ficha, chunks)
    print(f"Fuente usada: {fuente}", flush=True)
    print(f"Detalle: {nota}", flush=True)
    print(f"Chars que llegan al LLM (tras techo {TDR_MAX_CHARS}): {len(tdr)}", flush=True)
    if len(tdr) < TDR_MIN_CHARS:
        print(
                f"handleAnalizar devolveria 422 sin_tdr (minimo {TDR_MIN_CHARS} chars).",
            flush=True,
        )
    print("\n----- INICIO TDR / fragmentos -----\n", flush=True)
    print(tdr if tdr else "(vacío)", flush=True)
    print("\n----- FIN TDR / fragmentos -----", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
