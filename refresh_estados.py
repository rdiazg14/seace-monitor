#!/usr/bin/env python3
"""
G1 — Frescura de estado (RAG v1, no toca embedding/buscar_tdr).

Relee el estado actual en el SEACE de contratos no-terminales, UPSERT de
{id, estado, estado_verificado_at}. Cadencia partida: todos los Vigentes
cada corrida; En Evaluación por lotes rotativos.

Uso:
  python refresh_estados.py
  python refresh_estados.py --dry-run --limit 30
  python refresh_estados.py --gc          # borra chunks de cierres >60d
"""
from __future__ import annotations

import argparse
import os
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright
from supabase import create_client

# ── Cargar .env ────────────────────────────────────────────────────────────────
_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SPA_URL      = "https://prod6.seace.gob.pe/buscador-publico/contrataciones"
API_DETALLE  = ("https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico"
                "/contrataciones/listar-completo")
BATCH_DB     = 100
DELAY_S      = 0.3
PAGE_DB      = 1_000
GC_DIAS      = 60

# idEstadoContrato validado en listar-completo (2026-08-16):
#   2 = Vigente, 3 = En Evaluación (87099, 87067), 4 = Culminado.
# Terminal SOLO por lista positiva. Un id/nombre nuevo del SEACE nunca dispara GC.
IDS_TERMINAL = {4}  # Culminado. Resto de IDs terminales: aún no observados.
NOMBRE_POR_ID = {
    2: "Vigente",
    3: "En Evaluación",
    4: "Culminado",
}
NOMBRES_TERMINAL = (
    "Culminado", "Cancelado", "Desierto", "Anulado", "Cerrado",
)


def es_terminal(id_estado: int, nom: str | None = None) -> bool:
    if id_estado in IDS_TERMINAL:
        return True
    if nom in NOMBRES_TERMINAL:
        return True
    return False


def paginar(supa, cols: str, estado: str, *,
            order_verificado: bool = False, limit: int = 0) -> list[dict]:
    """Pagina de a 1000. limit=0 → todos."""
    out: list[dict] = []
    offset = 0
    while True:
        take = PAGE_DB if not limit else min(PAGE_DB, limit - len(out))
        if take <= 0:
            break
        q = (
            supa.table("contratos")
            .select(cols)
            .eq("estado", estado)
        )
        if order_verificado:
            q = q.order("estado_verificado_at", desc=False, nullsfirst=True)
        else:
            q = q.order("id")
        res = q.range(offset, offset + take - 1).execute()
        batch = res.data or []
        out.extend(batch)
        if len(batch) < take:
            break
        offset += take
        if limit and len(out) >= limit:
            break
    return out[:limit] if limit else out


def seleccionar_lote(supa, max_evaluacion: int, limit: int) -> list[dict]:
    """Todos los Vigentes, luego En Evaluación rotativo. --limit recorta el total."""
    cols = "id, estado, estado_verificado_at"
    vigentes = paginar(supa, cols, "Vigente")
    cupo_eval = max_evaluacion
    if limit:
        cupo_vig = min(len(vigentes), limit)
        vigentes = vigentes[:cupo_vig]
        cupo_eval = max(0, min(max_evaluacion, limit - len(vigentes)))
    evaluacion = paginar(
        supa, cols, "En Evaluación",
        order_verificado=True, limit=cupo_eval,
    ) if cupo_eval else []
    print(f"  Cadencia: vigentes={len(vigentes):,}  "
          f"en_evaluación={len(evaluacion):,}  "
          f"(max-evaluacion={max_evaluacion:,})", flush=True)
    return vigentes + evaluacion


def fetch_estado(page, contrato_id: int, retries: int = 2) -> dict | None:
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = page.request.get(
                API_DETALLE,
                params={"id_contrato": contrato_id},
                timeout=30_000,
            )
            if r.status != 200:
                last_err = f"HTTP {r.status}"
                time.sleep(1.0 * (attempt + 1))
                continue
            proj = (r.json() or {}).get("uitContratoCompletoProjection") or {}
            if not proj:
                last_err = "projection vacía"
                return None
            id_estado = proj.get("idEstadoContrato")
            if id_estado is None:
                last_err = "sin idEstadoContrato"
                return None
            nom = (proj.get("nomEstadoContrato") or "").strip() or None
            return {
                "id_estado": int(id_estado),
                "nom_estado": nom or NOMBRE_POR_ID.get(int(id_estado)),
            }
        except Exception as e:
            last_err = str(e)
            print(f"    [error] fetch_estado({contrato_id}) intento {attempt+1}: {e}",
                  flush=True)
            time.sleep(1.0 * (attempt + 1))
    print(f"    [error] fetch_estado({contrato_id}): {last_err}", flush=True)
    return None


def upsert_lote(supa, lote: list[dict]):
    supa.table("contratos").upsert(lote, on_conflict="id").execute()


def borrar_chunks(supa, contrato_id: int) -> int:
    res = (
        supa.table("chunks_tdr")
        .delete()
        .eq("contrato_id", contrato_id)
        .execute()
    )
    return len(res.data or [])


def gc_cierres_antiguos(supa, dry_run: bool) -> int:
    """
    GC acotado (solo con --gc).

    No usamos fecha_fin_cotizacion: es el fin de la ventana de cotización, no
    el cierre del contrato. La antigüedad del cierre es estado_verificado_at
    (momento en que G1 registró el paso a terminal). Los chunks se borran
    60 días después de ese timestamp; los cierres recientes se conservan
    para el chat. idx_chunks_contrato_id cubre el DELETE.
    """
    corte = (datetime.now(timezone.utc) - timedelta(days=GC_DIAS)).isoformat()
    out: list[dict] = []
    offset = 0
    while True:
        q = (
            supa.table("contratos")
            .select("id, estado, estado_verificado_at")
            .in_("estado", list(NOMBRES_TERMINAL))
            .lt("estado_verificado_at", corte)
            .not_.is_("estado_verificado_at", "null")
            .order("id")
            .range(offset, offset + PAGE_DB - 1)
        )
        batch = (q.execute().data) or []
        out.extend(batch)
        if len(batch) < PAGE_DB:
            break
        offset += PAGE_DB

    borrados = 0
    print(f"  GC candidatos (cierre >{GC_DIAS}d): {len(out):,}", flush=True)
    for row in out:
        cid = int(row["id"])
        if dry_run:
            print(f"    [dry-run] GC chunks contrato_id={cid} "
                  f"estado={row.get('estado')}", flush=True)
            continue
        try:
            n = borrar_chunks(supa, cid)
            borrados += n
            if n:
                print(f"    GC id={cid} chunks={n}", flush=True)
        except Exception as e:
            print(f"    [error] GC id={cid}: {e}", flush=True)
    return borrados


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="Tope global (0 = sin tope). Recorta vigentes primero.")
    ap.add_argument("--max-evaluacion", type=int, default=4000,
                    help="Tope de En Evaluación por corrida (default 4000)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Consulta API y loguea; no escribe en BD")
    ap.add_argument("--gc", action="store_true",
                    help="Borra chunks de cierres con estado_verificado_at >60d")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        raise SystemExit("ERROR: SUPABASE_URL / SUPABASE_SERVICE_KEY no encontrados")

    supa = create_client(SUPABASE_URL, SUPABASE_KEY)
    now_iso = datetime.now(timezone.utc).isoformat()

    print("=" * 60, flush=True)
    print("G1 — Refresco de estados", flush=True)
    print(f"  dry-run={args.dry_run}  gc={args.gc}  "
          f"limit={args.limit or '∞'}  max-evaluacion={args.max_evaluacion}",
          flush=True)
    print("=" * 60, flush=True)

    lote = seleccionar_lote(supa, args.max_evaluacion, args.limit)
    total = len(lote)
    if total == 0:
        print("Nada que refrescar.", flush=True)
        return

    t0 = time.time()
    ok = 0
    errores = 0
    sin_cambio = 0
    cambios_abiertos = 0
    cerrados: Counter[str] = Counter()
    ids_api: Counter[int] = Counter()
    pendiente: list[dict] = []

    def flush_upsert():
        if not pendiente or args.dry_run:
            pendiente.clear()
            return
        try:
            upsert_lote(supa, pendiente)
            print(f"    → upsert lote {len(pendiente)} filas OK", flush=True)
        except Exception as e:
            print(f"    → upsert lote FALLÓ: {e}", flush=True)
        pendiente.clear()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_context(ignore_https_errors=True).new_page()
        print("Iniciando sesión SEACE...", flush=True)
        page.goto(SPA_URL, wait_until="networkidle", timeout=90_000)
        page.wait_for_timeout(2_000)
        print("Sesión lista.\n", flush=True)

        for i, row in enumerate(lote, 1):
            cid = int(row["id"])
            estado_bd = row.get("estado")
            info = fetch_estado(page, cid)
            if not info:
                errores += 1
            else:
                id_est = info["id_estado"]
                nom = info["nom_estado"] or NOMBRE_POR_ID.get(id_est) or f"id:{id_est}"
                ids_api[id_est] += 1
                cambio = estado_bd != nom
                flag = "DIFERE" if cambio else "igual"
                if args.dry_run:
                    print(f"  [{i}/{total}] id={cid} BD={estado_bd!r} "
                          f"API={nom!r} (id={id_est}) {flag}", flush=True)
                elif i % 50 == 0 or cambio or i == total:
                    print(f"  [{i}/{total}] id={cid} {flag} "
                          f"{estado_bd} → {nom}", flush=True)
                if not args.dry_run:
                    pendiente.append({
                        "id": cid,
                        "estado": nom,
                        "estado_verificado_at": now_iso,
                    })
                if es_terminal(id_est, nom):
                    cerrados[nom] += 1
                elif cambio:
                    cambios_abiertos += 1
                else:
                    sin_cambio += 1
                ok += 1

            if len(pendiente) >= BATCH_DB:
                flush_upsert()
            time.sleep(DELAY_S)

        browser.close()

    flush_upsert()

    gc_borrados = 0
    if args.gc:
        print("\nGarbage collection (--gc)...", flush=True)
        gc_borrados = gc_cierres_antiguos(supa, args.dry_run)

    elapsed = time.time() - t0
    print(f"\n{'='*60}", flush=True)
    print(f"G1 completado en {elapsed:.0f}s", flush=True)
    print(f"  Revisados     : {ok:,}", flush=True)
    print(f"  Sin cambio    : {sin_cambio:,}", flush=True)
    print(f"  Cambio abierto: {cambios_abiertos:,}", flush=True)
    print(f"  Errores       : {errores:,}", flush=True)
    print(f"  Cerrados (id terminal): {sum(cerrados.values()):,}", flush=True)
    if cerrados:
        for nom, n in cerrados.most_common():
            print(f"    → {nom}: {n:,}", flush=True)
    print(f"  idEstadoContrato vistos: {dict(ids_api)}", flush=True)
    print(f"  Chunks borrados (GC) : {gc_borrados:,}", flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    main()
