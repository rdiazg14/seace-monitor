#!/usr/bin/env python3
"""Coherencia de capas. Read-only. Pensado para Actions (sin DSN).

Imprime: [capas] sin_clasificar=N capa_null=N c1=N sin_chunks=N
Exit 1 si capa_null > 0 (G3).
sin_chunks = postulables con PDF/OCR procesado y cero filas en chunks_tdr.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
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

from clasificacion_capa import conectar_pg  # noqa: E402

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
TRIGGER_STALE_H = 36


def _init_supa():
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()
    if not url or not key:
        return None
    from supabase import create_client
    return create_client(url, key)


def _paginar(
    supa,
    table: str,
    select: str,
    *,
    order: str,
    eq: dict | None = None,
) -> list[dict]:
    """Range estable: sin ORDER BY PostgREST salta filas y fabrica diffs falsos."""
    out: list[dict] = []
    offset = 0
    while True:
        q = supa.table(table).select(select).order(order)
        if eq:
            for col, val in eq.items():
                q = q.eq(col, val)
        res = q.range(offset, offset + PAGE - 1).execute()
        batch = res.data or []
        out.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return out


SQL_SIN_CHUNKS = """
SELECT count(*)::int AS n
FROM v_contratos_estado v
JOIN contratos c ON c.id = v.id
WHERE v.es_postulable
  AND (
    (c.tdr_texto IS NOT NULL AND btrim(c.tdr_texto) <> '')
    OR COALESCE(c.tdr_n_paginas_ocr, 0) > 0
    OR (
      jsonb_typeof(COALESCE(c.paginas_ocr_hechas, '[]'::jsonb)) = 'array'
      AND jsonb_array_length(COALESCE(c.paginas_ocr_hechas, '[]'::jsonb)) > 0
    )
  )
  AND NOT EXISTS (
    SELECT 1 FROM chunks_tdr ct WHERE ct.contrato_id = v.id
  )
"""


def _pdf_procesado(row: dict) -> bool:
    tdr = (row.get("tdr_texto") or "").strip()
    if tdr:
        return True
    if int(row.get("tdr_n_paginas_ocr") or 0) > 0:
        return True
    hechas = row.get("paginas_ocr_hechas")
    if isinstance(hechas, list) and len(hechas) > 0:
        return True
    return False


def via_pg() -> tuple[int, int, int, int] | None:
    conn = conectar_pg()
    if conn is None:
        return None
    try:
        sin_clasificar = conn.execute(
            """
            SELECT count(*)::int AS n
            FROM contratos c
            LEFT JOIN clasificacion_contrato cl ON cl.contrato_id = c.id
            WHERE cl.contrato_id IS NULL
            """
        ).fetchone()["n"]
        capa_null = conn.execute(
            "SELECT count(*)::int AS n FROM clasificacion_contrato WHERE capa IS NULL"
        ).fetchone()["n"]
        c1 = conn.execute(
            "SELECT count(*)::int AS n FROM clasificacion_contrato "
            "WHERE capa = 'gemini' AND contrato_id = ANY(%s)",
            (IDS_C1,),
        ).fetchone()["n"]
        sin_chunks = conn.execute(SQL_SIN_CHUNKS).fetchone()["n"]
        return int(sin_clasificar), int(capa_null), int(c1), int(sin_chunks)
    finally:
        conn.close()


def _count(supa, table: str, *, extra_select: str = "", extra_filter=None) -> int:
    """Count exact de una tabla via PostgREST."""
    select = "id" if not extra_select else extra_select
    q = supa.table(table).select(select, count="exact").limit(0)
    if extra_filter:
        q = extra_filter(q)
    return q.execute().count or 0


def via_supa(supa) -> tuple[int, int, int, int]:
    total_contratos = _count(supa, "contratos")
    total_clasificados = _count(supa, "clasificacion_contrato", extra_select="contrato_id")
    sin_clasificar = total_contratos - total_clasificados

    capa_null = _count(
        supa,
        "clasificacion_contrato",
        extra_select="contrato_id",
        extra_filter=lambda q: q.is_("capa", "null"),
    )

    res_c1 = (
        supa.table("clasificacion_contrato")
        .select("contrato_id")
        .eq("capa", "gemini")
        .in_("contrato_id", [int(x) for x in IDS_C1])
        .execute()
    )
    c1 = len(res_c1.data or [])

    # tdr_* no viven en v_contratos_estado (solo flags). Mismo split
    # que analizar_postulables.cargar_postulables_rest.
    postulables = _paginar(
        supa,
        "v_contratos_estado",
        "id",
        order="id",
        eq={"es_postulable": True},
    )
    ids_post = [int(r["id"]) for r in postulables]
    con_pdf: set[int] = set()
    for i in range(0, len(ids_post), 80):
        lote = ids_post[i : i + 80]
        res = (
            supa.table("contratos")
            .select("id,tdr_texto,tdr_n_paginas_ocr,paginas_ocr_hechas")
            .in_("id", lote)
            .order("id")
            .execute()
        )
        for row in res.data or []:
            if _pdf_procesado(row):
                con_pdf.add(int(row["id"]))
    con_chunk: set[int] = set()
    ids_pdf = list(con_pdf)
    for i in range(0, len(ids_pdf), 80):
        lote = ids_pdf[i:i + 80]
        offset = 0
        while True:
            res = (
                supa.table("chunks_tdr")
                .select("contrato_id")
                .in_("contrato_id", lote)
                .order("id")
                .range(offset, offset + PAGE - 1)
                .execute()
            )
            batch = res.data or []
            for row in batch:
                con_chunk.add(int(row["contrato_id"]))
            if len(batch) < PAGE:
                break
            offset += PAGE
    sin_chunks = len(con_pdf - con_chunk)
    return sin_clasificar, capa_null, c1, sin_chunks


def _github_token() -> str:
    t = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
    if t:
        return t
    try:
        r = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            return (r.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def horas_ultimo_run(event: str) -> float | None:
    """Horas desde el último run de pipeline.yml con ese event.

    El Worker dispara workflow_dispatch (usa GITHUB_PAT). El schedule: de
    GitHub no usa el PAT: si solo miráramos schedule, un token vencido no
    saltaría. Se reportan ambos; el exit 1 por atraso usa dispatch.
    """
    token = _github_token()
    if not token:
        return None
    repo = (os.getenv("GITHUB_REPOSITORY") or "rdiazg14/seace-monitor").strip()
    q = (
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        f"pipeline.yml/runs?event={event}&per_page=1"
    )
    req = urllib.request.Request(
        q,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "seace-verificar-capas",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            payload = json.loads(res.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as e:
        print(f"[trigger] aviso: no se pudo leer runs event={event}: {e}", flush=True)
        return None
    runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
    if not runs:
        return None
    created = runs[0].get("created_at")
    if not isinstance(created, str):
        return None
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


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
    sin_clasificar, capa_null, c1, sin_chunks = nums
    linea = (
        f"[capas] sin_clasificar={sin_clasificar} "
        f"capa_null={capa_null} c1={c1} sin_chunks={sin_chunks}"
    )
    print(linea, flush=True)
    print(f"[capas] backend={origen}", flush=True)
    persistir(linea)

    dispatch_h = horas_ultimo_run("workflow_dispatch")
    schedule_h = horas_ultimo_run("schedule")
    def _fmt(v: float | None) -> str:
        return "na" if v is None else f"{v:.1f}"
    print(
        f"[trigger] last_dispatch_h={_fmt(dispatch_h)} "
        f"last_schedule_h={_fmt(schedule_h)} stale_si>{TRIGGER_STALE_H}",
        flush=True,
    )
    trigger_stale = dispatch_h is not None and dispatch_h > TRIGGER_STALE_H
    if trigger_stale:
        print(
            f"[trigger] ALERTA: último workflow_dispatch hace {dispatch_h:.1f} h "
            f"(>{TRIGGER_STALE_H}). El Worker/PAT puede estar muerto. "
            f"schedule={_fmt(schedule_h)} h (el cron de GitHub no usa el PAT).",
            flush=True,
        )
    return 1 if capa_null > 0 or trigger_stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
