#!/usr/bin/env python3
"""Cache de PDFs en Storage (bucket privado tdr).

Ruta: tdr/{YYYY}/{MM}/{contrato_id}/{pdf_archivo_id}.pdf
YYYY/MM = mes de fecha_publica en America/Lima.

Idempotente: si pdf_storage_path ya es el arbol nuevo, skip (salvo --forzar).
El pipeline (descargar_requerimiento.py) sube el binario que ya tiene en
memoria; este script es el backfill / migracion.

  uv run python scripts/subir_pdf_storage.py --desde 2026-06-08 --dry-run
  uv run python scripts/subir_pdf_storage.py --desde 2026-06-08
  uv run python scripts/subir_pdf_storage.py --migrar-arbol
  uv run python scripts/subir_pdf_storage.py --solo-postulables
  uv run python scripts/subir_pdf_storage.py --ids 91696 --limit 1
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from supabase import create_client

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from descargar_requerimiento import (  # noqa: E402
    BUCKET_TDR,
    DESCARGAR_URL,
    MAX_PDF_STORAGE_BYTES,
    SeaceHttp,
    es_ruta_arbol_tdr,
    pdf_storage_ruta,
)

_ENV = _ROOT / ".env"
BUCKET = BUCKET_TDR
DELAY_S = 1.0
MAX_ERRORES_SEGUIDOS = 10
PROGRESO_CADA = 50


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
    desde: datetime | None,
    limit: int,
) -> list[dict]:
    if ids:
        rows = conn.execute(
            """
            SELECT
              c.id,
              c.pdf_archivo_id,
              c.pdf_storage_path,
              c.pdf_storage_bytes,
              c.pdf_nombre,
              c.fecha_publica,
              v.es_postulable,
              v.es_por_abrir
            FROM contratos c
            LEFT JOIN v_contratos_estado v ON v.id = c.id
            WHERE c.id = ANY(%s)
            ORDER BY c.id
            """,
            (ids,),
        ).fetchall()
    else:
        sql = """
            SELECT
              c.id,
              c.pdf_archivo_id,
              c.pdf_storage_path,
              c.pdf_storage_bytes,
              c.pdf_nombre,
              c.fecha_publica,
              v.es_postulable,
              v.es_por_abrir
            FROM contratos c
            LEFT JOIN v_contratos_estado v ON v.id = c.id
            WHERE c.estado = 'Vigente'
              AND c.pdf_archivo_id IS NOT NULL
        """
        params: list = []
        if desde is not None:
            sql += " AND c.fecha_publica >= %s"
            params.append(desde)
        if solo_postulables:
            sql += " AND (v.es_postulable OR v.es_por_abrir)"
        sql += " ORDER BY c.fecha_publica ASC NULLS LAST, c.id"
        rows = conn.execute(sql, params).fetchall()
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
    if len(body) > MAX_PDF_STORAGE_BYTES:
        raise RuntimeError(f"PDF {len(body)} bytes supera el tope del bucket ({MAX_PDF_STORAGE_BYTES})")
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


def migrar_arbol(conn: psycopg.Connection, supa) -> int:
    """Mueve tdr/{id}/{aid}.pdf -> tdr/{YYYY}/{MM}/{id}/{aid}.pdf."""
    rows = conn.execute(
        """
        SELECT id, pdf_archivo_id, pdf_storage_path, pdf_storage_bytes, fecha_publica
        FROM contratos
        WHERE pdf_storage_path IS NOT NULL
        ORDER BY id
        """
    ).fetchall()
    movidos = 0
    for row in rows:
        vieja = (row["pdf_storage_path"] or "").strip()
        if es_ruta_arbol_tdr(vieja):
            print(f"  id={row['id']} ya arbol {vieja}", flush=True)
            continue
        cid = int(row["id"])
        aid = int(row["pdf_archivo_id"])
        nueva = pdf_storage_ruta(cid, aid, row["fecha_publica"])
        print(f"  id={cid} {vieja} -> {nueva}", flush=True)
        data = supa.storage.from_(BUCKET).download(vieja)
        if not data:
            raise RuntimeError(f"download vacio {vieja}")
        subir(supa, nueva, data)
        marcar(conn, cid, nueva, len(data) if isinstance(data, (bytes, bytearray)) else int(row["pdf_storage_bytes"] or 0))
        try:
            supa.storage.from_(BUCKET).remove([vieja])
        except Exception as e:
            print(f"  [warn] no se borro {vieja}: {_redactar(str(e))}", flush=True)
        movidos += 1
    print(f"migrar_arbol movidos={movidos} revisados={len(rows)}", flush=True)
    return movidos


def _flag(row: dict) -> str:
    if row.get("es_postulable"):
        return "postulable"
    if row.get("es_por_abrir"):
        return "por_abrir"
    return "vigente"


def main() -> int:
    ap = argparse.ArgumentParser(description="Sube PDFs de SEACE al bucket tdr")
    ap.add_argument(
        "--solo-postulables",
        action="store_true",
        default=False,
        help="Solo es_postulable o es_por_abrir",
    )
    ap.add_argument(
        "--desde",
        default="",
        help="fecha_publica >= YYYY-MM-DD (universo: vigentes con pdf_archivo_id)",
    )
    ap.add_argument("--ids", default="", help="Lista explicita id,id,id")
    ap.add_argument("--limit", type=int, default=0, help="Tope de filas (0 = sin tope)")
    ap.add_argument("--dry-run", action="store_true", help="Lista, no descarga ni sube")
    ap.add_argument("--forzar", action="store_true", help="Re-sube aunque ya haya path")
    ap.add_argument(
        "--migrar-arbol",
        action="store_true",
        help="Mueve objetos de ruta plana al arbol YYYY/MM",
    )
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
    desde: datetime | None = None
    if args.desde:
        try:
            d = date.fromisoformat(args.desde.strip())
        except ValueError:
            print("ERROR: --desde debe ser YYYY-MM-DD", flush=True)
            return 2
        desde = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)

    if not ids and not args.solo_postulables and desde is None and not args.migrar_arbol:
        print(
            "ERROR: indica --desde YYYY-MM-DD, --solo-postulables, --ids o --migrar-arbol",
            flush=True,
        )
        return 2

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        supa = None if args.dry_run else create_client(url, key)
        if args.migrar_arbol:
            if args.dry_run:
                filas = conn.execute(
                    """
                    SELECT id, pdf_storage_path, fecha_publica, pdf_archivo_id
                    FROM contratos WHERE pdf_storage_path IS NOT NULL
                    ORDER BY id
                    """
                ).fetchall()
                n_plano = 0
                for r in filas:
                    nueva = pdf_storage_ruta(int(r["id"]), int(r["pdf_archivo_id"]), r["fecha_publica"])
                    if es_ruta_arbol_tdr(r["pdf_storage_path"]):
                        print(f"  id={r['id']} ya {r['pdf_storage_path']}", flush=True)
                    else:
                        n_plano += 1
                        print(f"  id={r['id']} DRY {r['pdf_storage_path']} -> {nueva}", flush=True)
                print(f"migrar_arbol dry planos={n_plano} total={len(filas)}", flush=True)
                return 0
            migrar_arbol(conn, supa)
            if not ids and not args.solo_postulables and desde is None:
                return 0

        cola = cargar_cola(
            conn,
            ids=ids,
            solo_postulables=args.solo_postulables,
            desde=desde,
            limit=args.limit,
        )

        avg_row = conn.execute(
            "SELECT coalesce(avg(pdf_storage_bytes), 0) AS avg_b "
            "FROM contratos WHERE pdf_storage_bytes > 0"
        ).fetchone()
        avg_b = float(avg_row["avg_b"] or 0)
        pendientes = [
            r for r in cola
            if r.get("pdf_archivo_id") is not None
            and (args.forzar or not es_ruta_arbol_tdr(r.get("pdf_storage_path")))
        ]
        est_bytes = avg_b * len(pendientes)
        est_s = len(pendientes) * DELAY_S
        print(
            f"cola={len(cola)} pendientes_subida={len(pendientes)} "
            f"dry_run={args.dry_run} forzar={args.forzar} "
            f"desde={args.desde or '-'} solo_postulables={args.solo_postulables} "
            f"avg_bytes={avg_b:.0f} est_mb={est_bytes / (1024 * 1024):.1f} "
            f"est_min={est_s / 60:.1f} DELAY_S={DELAY_S} "
            f"DESCARGAR_URL={DESCARGAR_URL}",
            flush=True,
        )

        intentados = subidos = saltados = fallidos = 0
        bytes_ok = 0
        seguidos = 0
        http: SeaceHttp | None = None
        t0 = time.monotonic()

        try:
            for i, row in enumerate(cola, 1):
                cid = int(row["id"])
                aid = row["pdf_archivo_id"]
                ya = row.get("pdf_storage_path")
                flag = _flag(row)
                if aid is None:
                    saltados += 1
                    print(
                        f"  [{i}/{len(cola)}] id={cid} SKIP sin pdf_archivo_id ({flag})",
                        flush=True,
                    )
                    continue
                aid = int(aid)
                path = pdf_storage_ruta(cid, aid, row.get("fecha_publica"))
                if es_ruta_arbol_tdr(ya) and not args.forzar:
                    saltados += 1
                    if i <= 15 or i % PROGRESO_CADA == 0:
                        print(
                            f"  [{i}/{len(cola)}] id={cid} SKIP ya cacheado {ya} ({flag})",
                            flush=True,
                        )
                    continue
                if args.dry_run:
                    intentados += 1
                    if i <= 15 or i % PROGRESO_CADA == 0:
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
                    if ya and ya != path:
                        try:
                            supa.storage.from_(BUCKET).remove([ya])
                        except Exception as e:
                            print(
                                f"  [warn] no se borro ruta vieja {ya}: {_redactar(str(e))}",
                                flush=True,
                            )
                    subidos += 1
                    bytes_ok += len(body)
                    seguidos = 0
                    print(
                        f"  [{i}/{len(cola)}] id={cid} OK {path} "
                        f"{len(body)} bytes ({flag})",
                        flush=True,
                    )
                except Exception as e:
                    fallidos += 1
                    seguidos += 1
                    print(
                        f"  [{i}/{len(cola)}] id={cid} FAIL {_redactar(str(e))} "
                        f"seguidos={seguidos}",
                        flush=True,
                    )
                    if seguidos >= MAX_ERRORES_SEGUIDOS:
                        print(
                            f"STOP {MAX_ERRORES_SEGUIDOS} errores seguidos; "
                            "posible bloqueo SEACE. Reanudar despues.",
                            flush=True,
                        )
                        break
                if subidos > 0 and subidos % PROGRESO_CADA == 0:
                    elapsed = time.monotonic() - t0
                    resto = len(pendientes) - subidos - fallidos
                    eta = (elapsed / subidos) * max(resto, 0)
                    print(
                        f"  -- progreso subidos={subidos} fallidos={fallidos} "
                        f"mb={bytes_ok / (1024 * 1024):.1f} "
                        f"elapsed={elapsed:.0f}s eta={eta / 60:.1f}min",
                        flush=True,
                    )
                time.sleep(DELAY_S)
        finally:
            if http is not None:
                http.close()

    mb = bytes_ok / (1024 * 1024)
    print(
        f"resumen intentados={intentados} subidos={subidos} "
        f"saltados={saltados} fallidos={fallidos} mb={mb:.2f} "
        f"stop_seguidos={seguidos >= MAX_ERRORES_SEGUIDOS}",
        flush=True,
    )
    if seguidos >= MAX_ERRORES_SEGUIDOS:
        return 1
    return 1 if fallidos else 0


if __name__ == "__main__":
    sys.exit(main())
