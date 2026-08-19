#!/usr/bin/env python3
"""Prueba puntual PDF → RAG v2.

  uv run python probar_pdf_rag.py
  uv run python probar_pdf_rag.py --ab --batch 1 --fail-fast
  uv run python probar_pdf_rag.py --pipeline --batch 1
"""
from __future__ import annotations

import argparse
import time

import httpx

from eval_retrieval import buscar_v2_vector, embed_query_gemini, create_client
from eval_retrieval import GEMINI_API_KEY, SUPABASE_KEY, SUPABASE_URL
from chunker_contratos import cuerpo_chunk, run_solo_pdf
from generar_embeddings import (
    QuotaExceeded,
    reset_embedding_v2,
    run_gemini,
    reset_embed_stats,
    print_embed_stats,
    EMBED_STATS,
)

MUESTRA = [87164, 87001, 87153, 87159, 87157]
AB_ID = 87164
IDS_CSV = ",".join(str(i) for i in MUESTRA)

QUERIES = [
    {
        "id": AB_ID,
        "q": "Sistema de Gestión Documental SGD registro de expedientes por ventanilla GRELL",
        "pista": "SGD",
    },
    {
        "id": AB_ID,
        "q": "impedimento nepotismo plazo ciento treinta y cinco días calendarios",
        "pista": "nepotismo",
        "pista2": "ciento treinta y cinco",
    },
]


def _as_vec(raw) -> list[float]:
    if isinstance(raw, str):
        return [float(x) for x in raw.strip("[]").split(",") if x]
    return [float(x) for x in raw]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def meta_cols_ok(supa) -> bool:
    try:
        supa.table("chunks_tdr").select("id,meta_entidad,meta_nro").limit(1).execute()
        return True
    except Exception:
        return False


def esperar_meta(supa, segundos: int = 45) -> bool:
    t0 = time.time()
    while time.time() - t0 < segundos:
        if meta_cols_ok(supa):
            return True
        print("  esperando chunks_pdf_meta.sql …", flush=True)
        time.sleep(5)
    return meta_cols_ok(supa)


def cobertura_pdf(supa, ids: list[int]) -> dict[int, tuple[int, int]]:
    out: dict[int, tuple[int, int]] = {}
    for cid in ids:
        tot = (
            supa.table("chunks_tdr")
            .select("id", count="exact")
            .eq("contrato_id", cid)
            .eq("fuente", "pdf")
            .limit(1)
            .execute()
        ).count or 0
        v2 = (
            supa.table("chunks_tdr")
            .select("id", count="exact")
            .eq("contrato_id", cid)
            .eq("fuente", "pdf")
            .not_.is_("embedding_v2", "null")
            .limit(1)
            .execute()
        ).count or 0
        out[cid] = (tot, v2)
    return out


def stats_vectores_pdf(supa, cid: int) -> dict:
    rows = (
        supa.table("chunks_tdr")
        .select("id,chunk_index,tipo,texto,embedding_v2")
        .eq("contrato_id", cid)
        .eq("fuente", "pdf")
        .not_.is_("embedding_v2", "null")
        .execute()
    ).data or []
    items = [(r, _as_vec(r["embedding_v2"])) for r in rows]
    n = len(items)
    pares: list[float] = []
    for i in range(n):
        for j in range(i + 1, n):
            pares.append(cosine(items[i][1], items[j][1]))
    mean_pair = sum(pares) / len(pares) if pares else None

    def find(pista: str):
        for r, v in items:
            if pista.lower() in (r.get("texto") or "").lower():
                return r, v
        return None, None

    sgd_r, sgd_v = find("SGD")
    nep_r, nep_v = find("nepotismo")
    sim_cruz = cosine(sgd_v, nep_v) if sgd_v and nep_v else None
    return {
        "n": n,
        "mean_pairwise": mean_pair,
        "sim_sgd_nepotismo": sim_cruz,
        "sgd_idx": None if not sgd_r else sgd_r["chunk_index"],
        "nep_idx": None if not nep_r else nep_r["chunk_index"],
    }


def print_stats(label: str, st: dict) -> None:
    if not st:
        print(f"  {label}: (sin datos)", flush=True)
        return
    mp = st.get("mean_pairwise")
    cr = st.get("sim_sgd_nepotismo")
    mp_s = f"{mp:.4f}" if isinstance(mp, float) else "—"
    cr_s = f"{cr:.4f}" if isinstance(cr, float) else "—"
    print(
        f"  {label}: n={st.get('n')}  mean_pairwise={mp_s}  "
        f"cos(SGD idx={st.get('sgd_idx')}, nepotismo idx={st.get('nep_idx')})={cr_s}",
        flush=True,
    )


def embed_query_once(http: httpx.Client, q: str) -> list[float]:
    try:
        vec = embed_query_gemini(http, q)
        EMBED_STATS["requests"] += 1
        EMBED_STATS["texts"] += 1
        EMBED_STATS["chars"] += len(q)
        return vec
    except Exception as e:
        if "429" in str(e):
            raise QuotaExceeded(f"429 embed query: {e}") from e
        raise


def evaluar_queries(
    supa,
    http: httpx.Client,
    label: str,
    fail_fast: bool,
    queries: list[dict] | None = None,
) -> list[dict]:
    resultados: list[dict] = []
    for i, item in enumerate(queries or QUERIES, 1):
        print("=" * 60, flush=True)
        print(f"[{label} {i}] esperado contrato={item['id']}", flush=True)
        print(f"    q={item['q']}", flush=True)
        try:
            vec = embed_query_once(http, item["q"])
        except QuotaExceeded:
            raise
        except Exception as e:
            print(f"    abort: {e}", flush=True)
            resultados.append({"label": label, "q": item["q"], "ok": False, "err": str(e)})
            if fail_fast:
                raise
            continue

        pdf_rows = (
            supa.table("chunks_tdr")
            .select("id,chunk_index,tipo,texto,embedding_v2")
            .eq("contrato_id", item["id"])
            .eq("fuente", "pdf")
            .not_.is_("embedding_v2", "null")
            .execute()
        ).data or []
        scored = []
        for r in pdf_rows:
            sim = cosine(vec, _as_vec(r["embedding_v2"]))
            scored.append((sim, r))
        scored.sort(key=lambda x: -x[0])
        print(f"    cosine directo vs {len(scored)} chunks pdf con v2:", flush=True)
        for sim, r in scored[:3]:
            blob = (r.get("texto") or "").lower()
            pista_hit = item["pista"].lower() in blob
            print(
                f"      idx={r['chunk_index']} {r['tipo']} sim={sim:.3f}"
                f"{' PISTA' if pista_hit else ''}",
                flush=True,
            )

        hits = buscar_v2_vector(supa, vec, k=10, min_sim=0.15)
        if not hits:
            print("    0 hits", flush=True)
            resultados.append({
                "label": label, "q": item["q"], "ok": False,
                "pdf_top5": False, "pista_top5": False, "rank": None,
            })
            if not fail_fast:
                time.sleep(2)
            continue

        ok_id = False
        ok_pdf = False
        ok_pista = False
        rank_pdf_pista: int | None = None
        for rank, h in enumerate(hits, 1):
            texto = h.get("texto") or ""
            pistas = [item["pista"]]
            if item.get("pista2"):
                pistas.append(item["pista2"])
            pista_aqui = any(p.lower() in texto.lower() for p in pistas)
            marca = ""
            if int(h.get("contrato_id") or 0) == item["id"]:
                ok_id = True
                marca += " ID"
            if (h.get("fuente") or "") == "pdf":
                ok_pdf = True
                marca += " PDF"
            if pista_aqui:
                ok_pista = True
                marca += " PISTA"
                if rank_pdf_pista is None and "PDF" in marca:
                    rank_pdf_pista = rank
            if rank <= 5 or marca:
                print(
                    f"    #{rank} cid={h.get('contrato_id')} "
                    f"idx={h.get('chunk_index')} "
                    f"fuente={h.get('fuente')} tipo={h.get('tipo')} "
                    f"sim={h.get('similarity'):.3f}{marca}",
                    flush=True,
                )
                if "PISTA" in marca or rank == 1:
                    print(f"       {texto[:280].replace(chr(10), ' | ')}", flush=True)

        pista_top5 = rank_pdf_pista is not None and rank_pdf_pista <= 5
        print(
            f"    resumen: contrato_en_top={ok_id}  "
            f"fuente_pdf_en_top={ok_pdf}  pista_en_texto={ok_pista}  "
            f"pdf+pista_top5={pista_top5} rank={rank_pdf_pista}",
            flush=True,
        )
        resultados.append({
            "label": label,
            "q": item["q"],
            "ok": True,
            "pdf_top5": any(
                (h.get("fuente") or "") == "pdf"
                and int(h.get("contrato_id") or 0) == item["id"]
                for h in hits[:5]
            ),
            "pista_top5": pista_top5,
            "rank": rank_pdf_pista,
            "best_direct": scored[0][0] if scored else None,
        })
        time.sleep(2 if fail_fast else 12)
    print("=" * 60, flush=True)
    return resultados


def run_ab(supa, batch: int, fail_fast: bool) -> tuple[str, dict]:
    reset_embed_stats()
    print("A/B header vs body  contrato", AB_ID, "(sin queries; ~24 embeds)", flush=True)
    ej = (
        supa.table("chunks_tdr")
        .select("texto")
        .eq("contrato_id", AB_ID)
        .eq("fuente", "pdf")
        .order("chunk_index")
        .limit(1)
        .execute()
    ).data or []
    if not ej:
        raise SystemExit("No hay chunks pdf de 87164. Corre chunker --solo-pdf --ids …")
    primera = (ej[0].get("texto") or "").split("\n", 1)[0]
    print(f"  header display={primera}", flush=True)
    print(f"  cuerpo[:80]={cuerpo_chunk(ej[0].get('texto') or '')[:80]!r}", flush=True)

    stats: dict[str, dict] = {}
    last_mode = None
    for mode in ("header", "body"):
        n = reset_embedding_v2(supa, [AB_ID], "pdf")
        print(f"\n>>> modo={mode}  reset {n} filas, embebiendo…", flush=True)
        try:
            run_gemini(
                supa, 0,
                fuente="pdf",
                ids=[AB_ID],
                batch=batch,
                embed_mode=mode,
                fail_fast=fail_fast,
                delay=0.2,
            )
        except QuotaExceeded as e:
            cob = cobertura_pdf(supa, [AB_ID])[AB_ID]
            raise QuotaExceeded(
                f"punto=1 A/B modo={mode}  87164 pdf v2={cob[1]}/{cob[0]}  {e}"
            ) from e
        st = stats_vectores_pdf(supa, AB_ID)
        stats[mode] = st
        print_stats(mode, st)
        last_mode = mode
        if st["n"] < 12:
            raise QuotaExceeded(
                f"punto=1 A/B modo={mode} incompleto n={st['n']}/12"
            )

    h = stats["header"]["mean_pairwise"] or 1.0
    b = stats["body"]["mean_pairwise"] or 1.0
    # Menor pairwise = menos colapso por header repetido.
    if abs(h - b) < 0.01:
        winner = "body"
        motivo = f"empate (~{h:.4f} vs {b:.4f}); se elige body"
    elif b < h:
        winner = "body"
        motivo = f"body pairwise {b:.4f} < header {h:.4f}"
    else:
        winner = "header"
        motivo = f"header pairwise {h:.4f} < body {b:.4f}"

    print(f"\nGANADOR A/B: {winner}  ({motivo})", flush=True)
    print_stats("header", stats["header"])
    print_stats("body", stats["body"])
    print_embed_stats("  A/B ")

    if winner != last_mode:
        print(f"  re-embebiendo 87164 en modo ganador={winner}", flush=True)
        reset_embedding_v2(supa, [AB_ID], "pdf")
        try:
            run_gemini(
                supa, 0,
                fuente="pdf",
                ids=[AB_ID],
                batch=batch,
                embed_mode=winner,
                fail_fast=fail_fast,
                delay=0.2,
            )
        except QuotaExceeded as e:
            raise QuotaExceeded(
                f"punto=1 A/B re-embed ganador={winner}  {e}"
            ) from e
    return winner, stats


def run_pipeline(supa, batch: int) -> None:
    print("=" * 60, flush=True)
    print("PIPELINE muestra PDF  fail-fast  ids=", MUESTRA, flush=True)
    print("=" * 60, flush=True)

    if esperar_meta(supa):
        print("  meta_entidad/meta_nro OK → re-chunk muestra", flush=True)
        run_solo_pdf(supa, MUESTRA, 0)
    else:
        print("  meta columnas AUN no están; sigo con header corto sin meta.", flush=True)

    winner = "body"
    stats: dict = {}
    try:
        winner, stats = run_ab(supa, batch, fail_fast=True)
    except QuotaExceeded as e:
        print(f"\nPARAR. {e}", flush=True)
        cob = cobertura_pdf(supa, MUESTRA)
        print("  cobertura:", cob, flush=True)
        return

    print("\n>>> paso 2: embeber muestra completa modo", winner, flush=True)
    try:
        run_gemini(
            supa, 0,
            fuente="pdf",
            ids=MUESTRA,
            batch=batch,
            embed_mode=winner,
            fail_fast=True,
        )
    except QuotaExceeded as e:
        cob = cobertura_pdf(supa, MUESTRA)
        print(f"\nPARAR. punto=2 embed muestra  {e}", flush=True)
        print("  cobertura:", cob, flush=True)
        print(f"  ganador A/B={winner}", flush=True)
        if stats:
            print_stats("header", stats.get("header") or {})
            print_stats("body", stats.get("body") or {})
        return

    cob = cobertura_pdf(supa, MUESTRA)
    print("  cobertura post-embed:", cob, flush=True)

    print("\n>>> paso 3: queries discriminativas", flush=True)
    sgd_ok = None
    try:
        with httpx.Client() as http:
            res = evaluar_queries(supa, http, winner, fail_fast=True)
        sgd = next((r for r in res if "SGD" in r.get("q", "")), None)
        sgd_ok = bool(sgd and sgd.get("pista_top5"))
    except QuotaExceeded as e:
        print(f"\nPARAR. punto=3 queries  {e}", flush=True)
        print(f"  ganador A/B={winner}", flush=True)
        if stats:
            print_stats("header", stats.get("header") or {})
            print_stats("body", stats.get("body") or {})
        return

    print("\n>>> paso 4: OCR 87157 (no bloquea 1-3)", flush=True)
    import subprocess
    import sys
    rc = subprocess.call(
        [sys.executable, "descargar_requerimiento.py", "--ids", "87157", "--rpm", "8"],
    )
    print(f"  OCR 87157 exit={rc}", flush=True)

    print("\n===== REPORTE =====", flush=True)
    print(f"ganador A/B: {winner}", flush=True)
    if stats:
        print_stats("header", stats.get("header") or {})
        print_stats("body", stats.get("body") or {})
    print(f"query SGD: chunk pdf correcto en top-5 = {'SÍ' if sgd_ok else 'NO'}", flush=True)
    nep = next((r for r in res if "nepotismo" in r.get("q", "")), None)
    if nep:
        print(
            f"query nepotismo/135d: pdf+pista_top5={nep.get('pista_top5')} "
            f"rank={nep.get('rank')}",
            flush=True,
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ab", action="store_true")
    ap.add_argument("--pipeline", action="store_true",
                    help="A/B 87164 → muestra 92 → queries → OCR 87157. Fail-fast 429.")
    ap.add_argument("--batch", type=int, default=1)
    ap.add_argument("--fail-fast", action="store_true")
    ap.add_argument("--solo-sgd", action="store_true",
                    help="Solo la query discriminativa SGD (paso A, corte).")
    args = ap.parse_args()

    if not GEMINI_API_KEY:
        raise SystemExit("GEMINI_API_KEY ausente")
    supa = create_client(SUPABASE_URL, SUPABASE_KEY)
    if args.pipeline:
        run_pipeline(supa, args.batch)
        return
    if args.ab:
        try:
            run_ab(supa, args.batch, fail_fast=args.fail_fast)
        except QuotaExceeded as e:
            print(f"PARAR. {e}", flush=True)
        return
    with httpx.Client() as http:
        try:
            qs = QUERIES[:1] if args.solo_sgd else QUERIES
            evaluar_queries(supa, http, "run", fail_fast=args.fail_fast, queries=qs)
        except QuotaExceeded as e:
            print(f"PARAR. punto=3 queries  {e}", flush=True)


if __name__ == "__main__":
    main()
