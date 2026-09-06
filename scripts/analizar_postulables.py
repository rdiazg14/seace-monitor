#!/usr/bin/env python3
"""Backfill de analisis_contrato para postulables actuales.

Llama POST /analizar del Worker (el Worker persiste). No escribe SQL.
No toca categoria_it ni contratos.

  uv run python scripts/analizar_postulables.py --dry-run
  uv run python scripts/analizar_postulables.py

Para a la primera 429 (cupo ANALYZE). Delay 3 s entre llamadas.
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

import psycopg

_ROOT = Path(__file__).resolve().parent.parent
_ENV = _ROOT / ".env"
PROMPT_VERSION = "1"
DELAY_S = 3.0
AI_PROXY_DEFAULT = "https://seace-ai-proxy.rdiazg14.workers.dev"


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


def cargar_postulables(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute(
        """
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
        """,
        (PROMPT_VERSION,),
    ).fetchall()
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


def llamar_analizar(proxy: str, contrato_id: int, timeout_s: float = 180.0) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{proxy.rstrip('/')}/analizar",
        data=json.dumps({"contrato_id": contrato_id}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        },
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill /analizar de postulables")
    ap.add_argument("--dry-run", action="store_true", help="Lista sin llamar al Worker")
    ap.add_argument("--limit", type=int, default=0, help="Tope de llamadas (0 = todos)")
    args = ap.parse_args()
    _cargar_env()

    dsn = (os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        print("ERROR: falta DATABASE_URL", file=sys.stderr)
        return 2
    proxy = (os.environ.get("AI_PROXY") or AI_PROXY_DEFAULT).rstrip("/")

    with psycopg.connect(dsn) as conn:
        filas = cargar_postulables(conn)

    ya = [r for r in filas if r["ya_en_bd"]]
    sin_texto = [r for r in filas if not r["ya_en_bd"] and not r["con_texto"]]
    candidatos = [r for r in filas if not r["ya_en_bd"] and r["con_texto"]]
    if args.limit > 0:
        candidatos = candidatos[: args.limit]

    print(
        f"postulables={len(filas)} ya_existentes={len(ya)} "
        f"sin_texto={len(sin_texto)} candidatos={len(candidatos)}",
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
    for i, r in enumerate(candidatos, 1):
        cid = r["id"]
        print(f"[{i}/{len(candidatos)}] POST /analizar id={cid} ...", flush=True)
        status, body = llamar_analizar(proxy, cid)
        if status == 429:
            err = str(body.get("error") or body.get("respuesta") or "429")
            print(f"  STOP cupo 429 {err}", flush=True)
            fallidos.append((cid, 429, err))
            pendientes = [x["id"] for x in candidatos[i - 1 :]]
            print(f"  pendientes_sin_llamar={pendientes}", flush=True)
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
    return 0 if not any(st == 429 for _, st, _ in fallidos) or analizados > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
