#!/usr/bin/env python3
"""Smoke front data paths: v_contratos + vistas KPI (mismo dato que Pages)."""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1] / "seace-monitor"
# allow running from seace-web or monitor
if not (_ROOT / ".env").is_file():
    _ROOT = Path(__file__).resolve().parent.parent
for line in (_ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def get(path: str):
    url = os.environ["SUPABASE_URL"].rstrip("/") + path
    anon = os.environ["SUPABASE_ANON_KEY"]
    req = urllib.request.Request(
        url,
        headers={"apikey": anon, "Authorization": f"Bearer {anon}"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def main() -> int:
    # postulables via v_contratos_estado (ya lee v_contratos)
    rows = get(
        "/rest/v1/v_contratos_estado?es_postulable=eq.true&select=id"
    )
    print("postulables", len(rows))
    kpis = get("/rest/v1/v_kpis_dashboard?select=*")
    print("kpis", json.dumps(kpis[0] if kpis else {}, ensure_ascii=False)[:300])
    # buscador filter Hardware
    q = urllib.parse.quote("categoria_it")
    hw = get(
        "/rest/v1/v_contratos?categoria_it=eq.Hardware&select=id,categoria_it&limit=3"
    )
    print("buscador_hardware_sample", hw)
    if len(rows) < 1:
        print("FAIL no postulables")
        return 1
    if not hw or hw[0].get("categoria_it") != "Hardware":
        print("FAIL filter categoria")
        return 1
    print("SMOKE_FRONT_DATA_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
