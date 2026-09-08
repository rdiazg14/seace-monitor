#!/usr/bin/env python3
"""Evalua keyword_candidatas (tipo A / tipo B). ARQUITECTURA_DATOS §11.

--dry-run: mide y lista; no INSERT it_keywords ni cambia estado.
Sin flag: activa solo las que pasan umbral (tipo A; tipo B con las 3).
"""
from __future__ import annotations

import argparse
import os
import sys
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

from supabase import create_client  # noqa: E402

from ingesta_completa import (  # noqa: E402
    _contiene,
    _texto_contrato,
    cargar_keywords,
    clasificar_categoria_it,
)
from clasificacion_capa import conectar_pg  # noqa: E402
from vocabulario import (  # noqa: E402
    MIN_VECES,
    UMBRAL_AUTO,
    es_tipo_a,
    keywords_incluye_activas,
)

PAGE = 1000


def init_supa():
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()
    if not url or not key:
        print("ERROR: falta SUPABASE_URL / SUPABASE_SERVICE_KEY", flush=True)
        return None
    return create_client(url, key)


def cargar_candidatas(supa) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while True:
        res = (
            supa.table("keyword_candidatas")
            .select("*")
            .in_("estado", ["nueva", "medida"])
            .order("id")
            .range(offset, offset + PAGE - 1)
            .execute()
        )
        batch = res.data or []
        out.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
    return out


def cargar_corpus(supa) -> list[dict]:
    """id + textos + categoria vigente (v_contratos = capa 3)."""
    out: list[dict] = []
    offset = 0
    cols = (
        "id,descripcion,descripcion_contrato,objeto,entidad,categoria_it"
    )
    while True:
        res = (
            supa.table("v_contratos")
            .select(cols)
            .order("id")
            .range(offset, offset + PAGE - 1)
            .execute()
        )
        batch = res.data or []
        out.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
        print(f"  corpus {len(out):,}", flush=True)
    return out


def fila_api(row: dict) -> dict:
    return {
        "desObjetoContrato": row.get("descripcion") or "",
        "desContratacion": row.get("descripcion_contrato") or "",
        "nomObjetoContrato": row.get("objeto") or "",
        "nomEntidad": row.get("entidad") or "",
    }


def preparar_corpus(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        api = fila_api(row)
        out.append({
            "row": row,
            "api": api,
            "t": _texto_contrato(api),
            "cat": row.get("categoria_it"),
        })
    return out


def medir_b_pg(conn, senal: str, categoria: str, cats) -> dict:
    ns = senal.strip().lower()
    rows = conn.execute(
        """
        SELECT id, descripcion, descripcion_contrato, objeto, entidad, categoria_it
        FROM v_contratos
        WHERE strpos(
          seace_norm(
            coalesce(descripcion, '') || ' ' ||
            coalesce(descripcion_contrato, '') || ' ' ||
            coalesce(objeto, '') || ' ' ||
            coalesce(entidad, '')
          ),
          %s
        ) > 0
        """,
        (ns,),
    ).fetchall()
    hits = [dict(r) for r in rows]
    universo_a = len(hits)
    ya = [r for r in hits if r.get("categoria_it")]
    cambios = 0
    cats_mod = _cats_con_candidata(cats, senal, categoria)
    for r in ya:
        nueva = clasificar_categoria_it(fila_api(r), cats_mod)
        if nueva and nueva != r.get("categoria_it"):
            cambios += 1
    ratio = (len(ya) / universo_a) if universo_a else None
    return {
        "universo_a": universo_a,
        "ya_etiquetados": len(ya),
        "cambios_categoria": cambios,
        "ratio_predictivo": None if ratio is None else round(ratio, 4),
    }


def medir_b(senal: str, categoria: str, corpus: list[dict], cats) -> dict:
    hits = [p for p in corpus if _contiene(p["t"], senal, False)]
    universo_a = len(hits)
    ya = [p for p in hits if p["cat"]]
    cambios = 0
    cats_mod = _cats_con_candidata(cats, senal, categoria)
    for p in ya:
        nueva = clasificar_categoria_it(p["api"], cats_mod)
        if nueva and nueva != p["cat"]:
            cambios += 1
    ratio = (len(ya) / universo_a) if universo_a else None
    return {
        "universo_a": universo_a,
        "ya_etiquetados": len(ya),
        "cambios_categoria": cambios,
        "ratio_predictivo": None if ratio is None else round(ratio, 4),
    }


def _cats_con_candidata(cats, senal: str, categoria: str):
    extra = {
        "keyword": senal,
        "tipo": "incluye",
        "limite_palabra": False,
        "tolera_plural": False,
    }
    out = []
    for cat, kws in cats or []:
        if cat == categoria:
            out.append((cat, list(kws) + [extra]))
        else:
            out.append((cat, kws))
    if cats and categoria not in {c for c, _ in cats}:
        out.append((categoria, [extra]))
    return out


def madre_tipo_a(senal: str, categoria: str, kws: list[dict]) -> tuple[dict | None, dict]:
    best = None
    best_ev: dict = {}
    for kw in kws:
        if kw.get("categoria") != categoria:
            continue
        ok, ev = es_tipo_a(senal, kw.get("keyword") or "")
        if not ok:
            continue
        # Preferir Levenshtein mas chico, luego keyword mas corta
        score = (ev.get("levenshtein", 99), len(kw.get("keyword") or ""))
        if best is None or score < best[0]:
            best = (score, kw)
            best_ev = ev
    return (best[1] if best else None), best_ev


def prioridad_categoria(supa, categoria: str) -> int:
    rows = (
        supa.table("it_keywords")
        .select("prioridad")
        .eq("categoria", categoria)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        return 99
    return int(rows[0]["prioridad"])


def insertar_keyword(supa, *, senal: str, categoria: str, madre: dict | None) -> int | None:
    pri = int(madre["prioridad"]) if madre and madre.get("prioridad") is not None else prioridad_categoria(supa, categoria)
    limite = bool(madre.get("limite_palabra")) if madre else False
    nota = "auto evaluar_candidatas.py tipo A" if madre else "auto evaluar_candidatas.py tipo B"
    res = (
        supa.table("it_keywords")
        .insert({
            "categoria": categoria,
            "keyword": senal,
            "prioridad": pri,
            "tipo": "incluye",
            "limite_palabra": limite,
            "activa": True,
            "tolera_plural": False,
            "nota": nota,
        })
        .execute()
    )
    rows = res.data or []
    if not rows:
        return None
    return int(rows[0]["id"])


def marcar_candidata(supa, cid: int, payload: dict) -> None:
    supa.table("keyword_candidatas").update(payload).eq("id", cid).execute()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    supa = init_supa()
    if supa is None:
        return 1
    cands = cargar_candidatas(supa)
    print(
        f"evaluar_candidatas dry_run={args.dry_run} n={len(cands)} "
        f"UMBRAL_AUTO={UMBRAL_AUTO} MIN_VECES={MIN_VECES}",
        flush=True,
    )
    if not cands:
        print("sin candidatas nueva/medida", flush=True)
        return 0

    kws = keywords_incluye_activas(supa)
    cats = cargar_keywords(supa)
    conn = conectar_pg()
    if conn is not None:
        print("cargando corpus via SQL...", flush=True)
        raw: list[dict] = []
        last_id = 0
        while True:
            batch = conn.execute(
                """
                SELECT id, descripcion, descripcion_contrato, objeto, entidad, categoria_it
                FROM contratos
                WHERE id > %s
                ORDER BY id
                LIMIT 8000
                """,
                (last_id,),
            ).fetchall()
            if not batch:
                break
            raw.extend(dict(r) for r in batch)
            last_id = int(batch[-1]["id"])
            print(f"  corpus {len(raw):,}", flush=True)
        corpus = preparar_corpus(raw)
        conn.close()
        conn = None
        print(f"corpus={len(corpus):,}", flush=True)
    else:
        print("cargando corpus (v_contratos via supabase)...", flush=True)
        corpus = preparar_corpus(cargar_corpus(supa))
        print(f"corpus={len(corpus):,}", flush=True)

    tipo_a: list[dict] = []
    tipo_b: list[dict] = []
    activar_a: list[dict] = []
    activar_b: list[dict] = []
    medida: list[dict] = []
    quedan_nueva: list[dict] = []

    now = datetime.now(timezone.utc).isoformat()
    for c in cands:
        senal = (c.get("senal") or "").strip()
        cat = (c.get("categoria_propuesta") or "").strip()
        veces = int(c.get("veces_vista") or 0)
        madre, ev_a = madre_tipo_a(senal, cat, kws)
        if madre:
            rec = {
                **c,
                "tipo_eval": "a",
                "madre": madre,
                "evidencia": ev_a,
                "activaria": True,
            }
            tipo_a.append(rec)
            activar_a.append(rec)
            continue
        met = medir_b(senal, cat, corpus, cats)
        rec = {**c, "tipo_eval": "b", **met}
        tipo_b.append(rec)
        ok = (
            met["universo_a"] <= UMBRAL_AUTO
            and met["cambios_categoria"] == 0
            and veces >= MIN_VECES
        )
        if ok:
            rec["activaria"] = True
            activar_b.append(rec)
        elif met["universo_a"] > UMBRAL_AUTO or met["cambios_categoria"] > 0:
            rec["activaria"] = False
            rec["destino"] = "medida"
            medida.append(rec)
        else:
            rec["activaria"] = False
            rec["destino"] = "nueva"
            quedan_nueva.append(rec)

    print(f"\ntipo_a={len(tipo_a)} tipo_b={len(tipo_b)}", flush=True)
    print(f"activarian_a={len(activar_a)} activarian_b={len(activar_b)}", flush=True)
    print(f"pasarian_a_medida={len(medida)} quedan_nueva={len(quedan_nueva)}", flush=True)
    print("\nid | tipo | senal | cat | veces | A | cambios | activaria", flush=True)
    for rec in tipo_a + tipo_b:
        print(
            f"  {rec.get('id')} | {rec['tipo_eval']} | {rec.get('senal')} | "
            f"{rec.get('categoria_propuesta')} | {rec.get('veces_vista')} | "
            f"{rec.get('universo_a', '-')} | {rec.get('cambios_categoria', '-')} | "
            f"{rec.get('activaria')}"
            + (
                f" madre={rec['madre'].get('keyword')}"
                if rec.get("madre")
                else ""
            ),
            flush=True,
        )

    if args.dry_run:
        print("\n[dry-run] no se activo ninguna keyword", flush=True)
        if conn is not None:
            conn.close()
        return 0

    n_act = 0
    for rec in activar_a:
        kid = insertar_keyword(
            supa,
            senal=rec["senal"],
            categoria=rec["categoria_propuesta"],
            madre=rec["madre"],
        )
        ev = {
            **(rec.get("evidencia") or {}),
            "UMBRAL_AUTO": UMBRAL_AUTO,
            "MIN_VECES": MIN_VECES,
            "keyword_madre_id": rec["madre"]["id"],
        }
        marcar_candidata(supa, rec["id"], {
            "estado": "auto_activada",
            "tipo_eval": "a",
            "keyword_madre_id": rec["madre"]["id"],
            "keyword_id": kid,
            "evaluada_utc": now,
            "activada_utc": now,
            "activada_por": "evaluar_candidatas.py",
            "evidencia": ev,
        })
        n_act += 1
    for rec in activar_b:
        kid = insertar_keyword(
            supa,
            senal=rec["senal"],
            categoria=rec["categoria_propuesta"],
            madre=None,
        )
        ev = {
            "universo_a": rec["universo_a"],
            "cambios_categoria": rec["cambios_categoria"],
            "ya_etiquetados": rec["ya_etiquetados"],
            "ratio_predictivo": rec["ratio_predictivo"],
            "UMBRAL_AUTO": UMBRAL_AUTO,
            "MIN_VECES": MIN_VECES,
        }
        marcar_candidata(supa, rec["id"], {
            "estado": "auto_activada",
            "tipo_eval": "b",
            "keyword_id": kid,
            "universo_a": rec["universo_a"],
            "cambios_categoria": rec["cambios_categoria"],
            "ya_etiquetados": rec["ya_etiquetados"],
            "ratio_predictivo": rec["ratio_predictivo"],
            "evaluada_utc": now,
            "activada_utc": now,
            "activada_por": "evaluar_candidatas.py",
            "evidencia": ev,
        })
        n_act += 1
    for rec in medida:
        marcar_candidata(supa, rec["id"], {
            "estado": "medida",
            "tipo_eval": "b",
            "universo_a": rec["universo_a"],
            "cambios_categoria": rec["cambios_categoria"],
            "ya_etiquetados": rec["ya_etiquetados"],
            "ratio_predictivo": rec["ratio_predictivo"],
            "evaluada_utc": now,
            "evidencia": {
                "universo_a": rec["universo_a"],
                "cambios_categoria": rec["cambios_categoria"],
                "UMBRAL_AUTO": UMBRAL_AUTO,
                "MIN_VECES": MIN_VECES,
            },
        })
    print(f"activadas={n_act} medidas={len(medida)}", flush=True)
    if conn is not None:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
