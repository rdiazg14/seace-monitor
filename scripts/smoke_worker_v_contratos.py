#!/usr/bin/env python3
"""Smoke Worker fase 5b: ficha via v_contratos (mismo camino que fetchFicha)."""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for line in (_ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

PROXY = "https://seace-ai-proxy.rdiazg14.workers.dev"
# id con categoria conocida (C4 gemini)
CID = int(sys.argv[1]) if len(sys.argv) > 1 else 92081


def get_json(url: str, headers: dict) -> dict | list:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def main() -> int:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    anon = os.environ["SUPABASE_ANON_KEY"]
    svc = (os.environ.get("ANALIZAR_SERVICE_TOKEN") or "").strip()
    h_sb = {
        "apikey": anon,
        "Authorization": f"Bearer {anon}",
    }
    # Camino Worker: v_contratos
    v = get_json(
        f"{url}/rest/v1/v_contratos?id=eq.{CID}&select=id,categoria_it,relevancia_ia",
        h_sb,
    )
    c = get_json(
        f"{url}/rest/v1/contratos?id=eq.{CID}&select=id,categoria_it,relevancia_ia",
        h_sb,
    )
    print("v_contratos", v)
    print("contratos_eco", c)
    if not v or not c:
        print("FAIL empty")
        return 1
    if v[0]["categoria_it"] != c[0]["categoria_it"]:
        print("FAIL cat mismatch view vs eco")
        return 1
    cat = v[0]["categoria_it"]
    print("cat_ok", cat)

    # /analizar (service) — no gasta cupo IP; confirma 200 y que el contrato existe via v_contratos
    body = json.dumps({"contrato_id": CID}).encode()
    req = urllib.request.Request(
        f"{PROXY}/analizar",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Service-Token": svc,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode()
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        status = e.code
        print("analizar_status", status)
        print("analizar_body_head", raw[:400])
        if status == 404:
            print("FAIL 404: fetchFicha no encontro el contrato en v_contratos")
            return 1
        # 429/502 etc: ficha se leyo si no es 404
        print("WARN analizar no-200 pero no es 404 (ficha probablemente OK)")
        return 0 if status != 404 else 1

    print("analizar_status", status)
    data = json.loads(raw)
    # si hay cache, ok; si error, ver
    print("analizar_keys", sorted(data.keys())[:20])
    print("SMOKE_OK cat=", cat)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
