#!/usr/bin/env python3
"""Backfill de analisis_contrato para postulables actuales.

Llama POST /analizar del Worker (el Worker persiste). No escribe SQL.
No toca categoria_it ni contratos.

Cola: v_contratos_estado.es_postulable + texto + no hay fila con el
pdf_hash actual y prompt_version. Si SEACE reemplaza el PDF, pdf_hash
cambia y el análisis viejo no cuenta: vuelve a entrar.

  uv run python scripts/analizar_postulables.py --dry-run
  uv run python scripts/analizar_postulables.py --limit 25

Manda X-Service-Token (ANALIZAR_SERVICE_TOKEN) para no gastar el cupo
por IP del navegador. El Worker sigue cobrando ANALYZE_RPD global.
Para a la primera 429/503 de cupo. Delay 3 s entre llamadas.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_ENV = _ROOT / ".env"
PROMPT_VERSION = "1"
DELAY_S = 3.0
AI_PROXY_DEFAULT = "https://seace-ai-proxy.rdiazg14.workers.dev"
_SQL_COLA = """
        SELECT
          c.id,
          c.nro_contratacion,
          c.entidad,
          c.pdf_hash,
          length(coalesce(c.tdr_texto, '')) AS tdr_len,
          (
            SELECT count(*)::int FROM chunks_tdr ch
            WHERE ch.contrato_id = c.id
          ) AS n_chunks,
          EXISTS (
            SELECT 1 FROM analisis_contrato a
            WHERE a.contrato_id = c.id
              AND a.pdf_hash = CASE
                WHEN nullif(btrim(coalesce(c.pdf_hash, '')), '') IS NULL THEN 'na'
                ELSE btrim(c.pdf_hash)
              END
              AND a.prompt_version = %s
          ) AS ya_en_bd
        FROM v_contratos_estado v
        JOIN contratos c ON c.id = v.id
        WHERE v.es_postulable
        ORDER BY c.fecha_fin_cotizacion ASC NULLS LAST, c.id
        """


def _cargar_env() -> None:
    if not _ENV.exists():
        return
    for line in _ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _pdf_hash(raw: object) -> str:
    s = (str(raw) if raw is not None else "").strip()
    return s or "na"


def _tiene_texto(tdr_len: int, n_chunks: int) -> bool:
    return tdr_len >= 200 or n_chunks > 0


def _map_rows(rows: list[tuple]) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        tdr_len = int(r[4] or 0)
        n_chunks = int(r[5] or 0)
        out.append({
            "id": int(r[0]),
            "nro": r[1],
            "entidad": (r[2] or "")[:80],
            "pdf_hash": _pdf_hash(r[3]),
            "tdr_len": tdr_len,
            "n_chunks": n_chunks,
            "ya_en_bd": bool(r[6]),
            "con_texto": _tiene_texto(tdr_len, n_chunks),
        })
    return out


def cargar_postulables_pg(dsn: str) -> list[dict]:
    import psycopg

    with psycopg.connect(dsn) as conn:
        rows = conn.execute(_SQL_COLA, (PROMPT_VERSION,)).fetchall()
    return _map_rows(rows)


def _rest_ids_postulables(supa) -> list[int]:
    ids: list[int] = []
    offset = 0
    page = 1000
    while True:
        res = (
            supa.table("v_contratos_estado")
            .select("id")
            .eq("es_postulable", True)
            .order("id")
            .range(offset, offset + page - 1)
            .execute()
        )
        batch = res.data or []
        ids.extend(int(r["id"]) for r in batch)
        if len(batch) < page:
            break
        offset += page
    return ids


def cargar_postulables_rest(supa) -> list[dict]:
    """Cola vía PostgREST (GitHub Actions si no hay DATABASE_URL)."""
    ids = _rest_ids_postulables(supa)
    if not ids:
        return []
    by_id: dict[int, dict] = {}
    for i in range(0, len(ids), 80):
        lote = ids[i : i + 80]
        contratos = (
            supa.table("contratos")
            .select(
                "id,nro_contratacion,entidad,pdf_hash,tdr_texto,"
                "fecha_fin_cotizacion"
            )
            .in_("id", lote)
            .execute()
        )
        analisis = (
            supa.table("analisis_contrato")
            .select("contrato_id,pdf_hash,prompt_version")
            .eq("prompt_version", PROMPT_VERSION)
            .in_("contrato_id", lote)
            .execute()
        )
        hashes: dict[int, set[str]] = {}
        for a in analisis.data or []:
            hashes.setdefault(int(a["contrato_id"]), set()).add(
                _pdf_hash(a.get("pdf_hash"))
            )
        con_chunk: set[int] = set()
        ch = (
            supa.table("chunks_tdr")
            .select("contrato_id")
            .in_("contrato_id", lote)
            .limit(1000)
            .execute()
        )
        for row in ch.data or []:
            con_chunk.add(int(row["contrato_id"]))
        for c in contratos.data or []:
            cid = int(c["id"])
            tdr = c.get("tdr_texto") or ""
            tdr_len = len(tdr)
            n_chunks = 1 if cid in con_chunk else 0
            actual = _pdf_hash(c.get("pdf_hash"))
            by_id[cid] = {
                "id": cid,
                "nro": c.get("nro_contratacion"),
                "entidad": (c.get("entidad") or "")[:80],
                "pdf_hash": actual,
                "tdr_len": tdr_len,
                "n_chunks": n_chunks,
                "ya_en_bd": actual in hashes.get(cid, set()),
                "con_texto": _tiene_texto(tdr_len, n_chunks),
                "fin": c.get("fecha_fin_cotizacion") or "",
            }
    ordered = [by_id[i] for i in ids if i in by_id]
    ordered.sort(key=lambda r: (r.get("fin") or "9999", r["id"]))
    for r in ordered:
        r.pop("fin", None)
    return ordered


def llamar_analizar(
    proxy: str,
    contrato_id: int,
    service_token: str,
    timeout_s: float = 180.0,
) -> tuple[int, dict]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    if service_token:
        headers["X-Service-Token"] = service_token
    req = urllib.request.Request(
        f"{proxy.rstrip('/')}/analizar",
        data=json.dumps({"contrato_id": contrato_id}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            body: dict
            try:
                parsed = json.loads(raw)
                body = parsed if isinstance(parsed, dict) else {"raw": raw[:300]}
            except json.JSONDecodeError:
                body = {"raw": raw[:300]}
            return int(resp.status), body
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        try:
            parsed = json.loads(raw)
            body = parsed if isinstance(parsed, dict) else {"raw": raw[:300]}
        except json.JSONDecodeError:
            body = {"raw": raw[:300], "error": str(e)}
        return int(e.code), body


def _es_cupo(status: int, body: dict) -> bool:
    if status == 429:
        return True
    if status == 503 and str(body.get("error") or "") == "over_capacity":
        return True
    detalle = str(body.get("detalle_tecnico") or "")
    if status in (429, 502) and "429" in detalle:
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill /analizar de postulables")
    ap.add_argument("--dry-run", action="store_true", help="Lista sin llamar al Worker")
    ap.add_argument("--limit", type=int, default=0, help="Tope de llamadas (0 = todos)")
    args = ap.parse_args()
    _cargar_env()

    proxy = (os.environ.get("AI_PROXY") or AI_PROXY_DEFAULT).rstrip("/")
    token = (os.environ.get("ANALIZAR_SERVICE_TOKEN") or "").strip()
    if not token:
        print(
            "WARN: falta ANALIZAR_SERVICE_TOKEN; el backfill comparte "
            "ANALYZE_IP_RPD con el navegador",
            flush=True,
        )

    dsn = (os.environ.get("DATABASE_URL") or "").strip()
    if dsn:
        filas = cargar_postulables_pg(dsn)
    else:
        url = (os.environ.get("SUPABASE_URL") or "").strip()
        key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
        if not url or not key:
            print(
                "ERROR: falta DATABASE_URL (o SUPABASE_URL + SUPABASE_SERVICE_KEY)",
                file=sys.stderr,
            )
            return 2
        from supabase import create_client

        filas = cargar_postulables_rest(create_client(url, key))

    ya = [r for r in filas if r["ya_en_bd"]]
    sin_texto = [r for r in filas if not r["ya_en_bd"] and not r["con_texto"]]
    candidatos = [r for r in filas if not r["ya_en_bd"] and r["con_texto"]]
    if args.limit > 0:
        candidatos = candidatos[: args.limit]

    print(
        f"postulables={len(filas)} ya_existentes={len(ya)} "
        f"sin_texto={len(sin_texto)} candidatos={len(candidatos)} "
        f"via_servicio={'si' if token else 'no'}",
        flush=True,
    )
    if sin_texto:
        print("sin_texto ids:", " ".join(str(r["id"]) for r in sin_texto), flush=True)
    print("--- candidatos ---", flush=True)
    for r in candidatos:
        print(
            f"  {r['id']} nro={r['nro']} tdr={r['tdr_len']} "
            f"chunks={r['n_chunks']} hash={r['pdf_hash'][:12]} "
            f"{r['entidad']}",
            flush=True,
        )
    if args.dry_run:
        print("dry-run: sin llamadas", flush=True)
        return 0

    analizados = 0
    fallidos: list[tuple[int, int, str]] = []
    cupo = False
    for i, r in enumerate(candidatos, 1):
        cid = r["id"]
        print(f"[{i}/{len(candidatos)}] POST /analizar id={cid} ...", flush=True)
        status, body = llamar_analizar(proxy, cid, token)
        if _es_cupo(status, body):
            err = str(body.get("error") or body.get("respuesta") or status)
            print(f"  STOP cupo {status} {err}", flush=True)
            fallidos.append((cid, status, err))
            pendientes = [x["id"] for x in candidatos[i - 1 :]]
            print(f"  pendientes_sin_llamar={pendientes}", flush=True)
            cupo = True
            break
        if status == 422 and body.get("status") == "sin_tdr":
            print(f"  422 sin_tdr tdr_chars={body.get('tdr_chars')}", flush=True)
            fallidos.append((cid, 422, "sin_tdr"))
        elif 200 <= status < 300 and body.get("analisis"):
            analizados += 1
            print(
                f"  OK cache={body.get('analizado_utc', '')[:19]} "
                f"fuente={body.get('tdr_fuente')}",
                flush=True,
            )
        else:
            msg = str(
                body.get("error")
                or body.get("mensaje")
                or body.get("detalle_tecnico")
                or status
            )[:180]
            print(f"  FAIL HTTP {status} {msg}", flush=True)
            fallidos.append((cid, status, msg))
        if i < len(candidatos):
            time.sleep(DELAY_S)

    print(
        f"listo candidatos={len(candidatos)} analizados={analizados} "
        f"ya_existentes={len(ya)} sin_texto={len(sin_texto)} "
        f"fallidos={len(fallidos)}",
        flush=True,
    )
    for cid, st, msg in fallidos:
        print(f"  fail id={cid} status={st} {msg}", flush=True)
    # 429/cupo: el step de Actions debe fallar (G3) aunque continue-on-error
    # deje seguir a reconciliar_funnel.
    return 1 if cupo else 0


if __name__ == "__main__":
    raise SystemExit(main())
