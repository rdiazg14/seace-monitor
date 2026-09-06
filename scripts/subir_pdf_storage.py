#!/usr/bin/env python3
"""B23 fase 1: cache de PDFs en Storage (bucket privado tdr).

Ruta: tdr/{contrato_id}/{pdf_archivo_id}.pdf
Idempotente: si pdf_storage_path ya esta, skip (salvo --forzar).
No toca el pipeline ni el front.

  uv run python scripts/subir_pdf_storage.py --solo-postulables --dry-run
  uv run python scripts/subir_pdf_storage.py --solo-postulables
  uv run python scripts/subir_pdf_storage.py --ids 91696 --limit 1
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from supabase import create_client

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from descargar_requerimiento import DESCARGAR_URL, SeaceHttp  # noqa: E402

_ENV = _ROOT / ".env"
BUCKET = "tdr"
DELAY_S = 1.0
MAX_BYTES = 52_428_800


def _cargar_env() -> None:
    if not _ENV.exists():
        return
    for line in _ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _redactar(texto: str) -> str:
    for key in ("DATABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_KEY"):
        val = os.environ.get(key) or ""
        if val:
            texto = texto.replace(val, "***")
    return texto


def storage_path(cid: int, aid: int) -> str:
    return f"tdr/{cid}/{aid}.pdf"


def es_pdf_real(body: bytes) -> bool:
    return body.lstrip().startswith(b"%PDF")


def parse_ids(raw: str) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for part in raw.replace(" ", "").split(","):
        if part:
            out.append(int(part))
    return out


def cargar_cola(
    conn: psycopg.Connection,
    *,
    ids: list[int],
    solo_postulables: bool,
    limit: int,
) -> list[dict]:
    if ids:
        rows = conn.execute(
            """
            SELECT
              c.id,
              c.pdf_archivo_id,
              c.pdf_storage_path,
              c.pdf_nombre,
              v.es_postulable,
              v.es_por_abrir
            FROM contratos c
            LEFT JOIN v_contratos_estado v ON v.id = c.id
            WHERE c.id = ANY(%s)
            ORDER BY c.id
            """,
            (ids,),
        ).fetchall()
    elif solo_postulables:
        rows = conn.execute(
            """
            SELECT
              c.id,
              c.pdf_archivo_id,
              c.pdf_storage_path,
              c.pdf_nombre,
              v.es_postulable,
              v.es_por_abrir
            FROM v_contratos_estado v
            JOIN contratos c ON c.id = v.id
            WHERE v.es_postulable OR v.es_por_abrir
            ORDER BY c.id
            """
        ).fetchall()
    else:
        print("ERROR: indica --solo-postulables (default) o --ids", flush=True)
        sys.exit(2)
    out = [dict(r) for r in rows]
    if limit and limit > 0:
        out = out[:limit]
    return out


def descargar_pdf(http: SeaceHttp, aid: int) -> bytes:
    url = DESCARGAR_URL.format(
        idContratoArchivo=aid,
        id=aid,
        id_archivo=aid,
    )
    status, headers, body = http.get_bytes(url)
    ctype = headers.get("content-type") or ""
    if status != 200:
        raise RuntimeError(f"HTTP {status} ({ctype[:80]})")
    if not body:
        raise RuntimeError("respuesta vacia")
    if not es_pdf_real(body):
        raise RuntimeError(
            f"no es PDF (magic={body[:8]!r} content-type={ctype[:80]} n={len(body)})"
        )
    if len(body) > MAX_BYTES:
        raise RuntimeError(f"PDF {len(body)} bytes supera el tope del bucket ({MAX_BYTES})")
    return body


def subir(supa, path: str, data: bytes) -> None:
    supa.storage.from_(BUCKET).upload(
        path,
        data,
        {"content-type": "application/pdf", "upsert": "true"},
    )


def marcar(conn: psycopg.Connection, cid: int, path: str, n_bytes: int) -> None:
    conn.execute(
        """
        UPDATE contratos
        SET pdf_storage_path = %s,
            pdf_storage_at = now(),
            pdf_storage_bytes = %s
        WHERE id = %s
        """,
        (path, n_bytes, cid),
    )
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser(description="Sube PDFs de SEACE al bucket tdr")
    ap.add_argument(
        "--solo-postulables",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Solo es_postulable o es_por_abrir (default: si)",
    )
    ap.add_argument("--ids", default="", help="Lista explicita id,id,id")
    ap.add_argument("--limit", type=int, default=0, help="Tope de filas (0 = sin tope)")
    ap.add_argument("--dry-run", action="store_true", help="Lista, no descarga ni sube")
    ap.add_argument("--forzar", action="store_true", help="Re-sube aunque ya haya path")
    args = ap.parse_args()

    _cargar_env()
    dsn = (os.environ.get("DATABASE_URL") or "").strip()
    url = (os.environ.get("SUPABASE_URL") or "").strip()
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    if not dsn:
        print("ERROR: falta DATABASE_URL", flush=True)
        return 2
    if not args.dry_run and (not url or not key):
        print("ERROR: falta SUPABASE_URL o SUPABASE_SERVICE_KEY", flush=True)
        return 2

    ids = parse_ids(args.ids)
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        cola = cargar_cola(
            conn,
            ids=ids,
            solo_postulables=args.solo_postulables,
            limit=args.limit,
        )

        print(
            f"cola={len(cola)} dry_run={args.dry_run} forzar={args.forzar} "
            f"DESCARGAR_URL={DESCARGAR_URL}",
            flush=True,
        )

        intentados = subidos = saltados = fallidos = 0
        bytes_ok = 0
        http: SeaceHttp | None = None
        supa = None if args.dry_run else create_client(url, key)

        try:
            for i, row in enumerate(cola, 1):
                cid = int(row["id"])
                aid = row["pdf_archivo_id"]
                ya = row.get("pdf_storage_path")
                flag = (
                    "postulable"
                    if row.get("es_postulable")
                    else "por_abrir"
                    if row.get("es_por_abrir")
                    else "otro"
                )
                if aid is None:
                    saltados += 1
                    print(
                        f"  [{i}/{len(cola)}] id={cid} SKIP sin pdf_archivo_id ({flag})",
                        flush=True,
                    )
                    continue
                aid = int(aid)
                path = storage_path(cid, aid)
                if ya and not args.forzar:
                    saltados += 1
                    print(
                        f"  [{i}/{len(cola)}] id={cid} SKIP ya cacheado {ya} ({flag})",
                        flush=True,
                    )
                    continue
                if args.dry_run:
                    intentados += 1
                    print(
                        f"  [{i}/{len(cola)}] id={cid} DRY {path} "
                        f"archivo={aid} nombre={(row.get('pdf_nombre') or '')[:60]!r} "
                        f"({flag})",
                        flush=True,
                    )
                    continue

                intentados += 1
                if http is None:
                    http = SeaceHttp()
                try:
                    body = descargar_pdf(http, aid)
                    subir(supa, path, body)
                    marcar(conn, cid, path, len(body))
                    subidos += 1
                    bytes_ok += len(body)
                    print(
                        f"  [{i}/{len(cola)}] id={cid} OK {path} "
                        f"{len(body)} bytes ({flag})",
                        flush=True,
                    )
                except Exception as e:
                    fallidos += 1
                    print(
                        f"  [{i}/{len(cola)}] id={cid} FAIL {_redactar(str(e))}",
                        flush=True,
                    )
                time.sleep(DELAY_S)
        finally:
            if http is not None:
                http.close()

    mb = bytes_ok / (1024 * 1024)
    print(
        f"resumen intentados={intentados} subidos={subidos} "
        f"saltados={saltados} fallidos={fallidos} mb={mb:.2f}",
        flush=True,
    )
    return 1 if fallidos else 0


if __name__ == "__main__":
    sys.exit(main())
