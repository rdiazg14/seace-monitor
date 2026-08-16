#!/usr/bin/env python3
"""
G3 — Alerta de confiabilidad.

Abre un GitHub issue si:
  - la corrida de Actions falló (--job-status failure), o
  - la API del SEACE vino vacía/rota (alerta_anomala=1 en data/ultima_ingesta.txt), o
  - se pide una prueba (--simular).

Anti-spam: si ya hay un issue [G3] abierto, comenta en él (no crea otro).
0 contratos nuevos en incremental normal NO alerta.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
LOG = ROOT / "data" / "ultima_ingesta.txt"


def _leer_log() -> dict[str, str]:
    out: dict[str, str] = {}
    if not LOG.exists():
        return out
    for line in LOG.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
        elif line.strip():
            out.setdefault("ts", line.strip())
    return out


def _gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )


def _issues_abiertos() -> list[dict]:
    r = _gh("issue", "list", "--state", "open", "--limit", "50",
            "--json", "number,title,url")
    if r.returncode != 0:
        raise SystemExit(f"gh issue list falló: {r.stderr or r.stdout}")
    try:
        return json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return []


def _g3_abierto(prueba: bool) -> dict | None:
    for it in _issues_abiertos():
        t = it.get("title") or ""
        if prueba:
            if t.startswith("[G3][prueba]"):
                return it
        elif t.startswith("[G3]") and "[prueba]" not in t:
            return it
    return None


def _comentar(numero: int, cuerpo: str, url: str) -> str:
    r = _gh("issue", "comment", str(numero), "--body", cuerpo)
    if r.returncode != 0:
        raise SystemExit(f"gh issue comment falló: {r.stderr or r.stdout}")
    print(f"Comentario en issue abierto: {url}", flush=True)
    return url


def _crear_issue(titulo: str, cuerpo: str) -> str:
    r = _gh("issue", "create", "--title", titulo, "--body", cuerpo)
    if r.returncode != 0:
        raise SystemExit(f"gh issue create falló: {r.stderr or r.stdout}")
    url = (r.stdout or "").strip()
    print(f"Issue abierto: {url}", flush=True)
    return url


def _publicar(titulo: str, cuerpo: str, prueba: bool) -> str:
    existente = _g3_abierto(prueba)
    if existente:
        n = int(existente["number"])
        extra = f"Falló otra vez — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n{cuerpo}"
        return _comentar(n, extra, existente.get("url") or f"#{n}")
    return _crear_issue(titulo, cuerpo)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-status", default="", help="success | failure | cancelled")
    ap.add_argument("--simular", action="store_true", help="Disparo de prueba [G3][prueba]")
    args = ap.parse_args()

    status = (args.job_status or os.environ.get("JOB_STATUS") or "").lower()
    log = _leer_log()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    run_url = ""
    if os.environ.get("GITHUB_SERVER_URL") and os.environ.get("GITHUB_REPOSITORY") and os.environ.get("GITHUB_RUN_ID"):
        run_url = (
            f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
            f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        )

    if args.simular:
        _publicar(
            f"[G3][prueba] alerta SEACE Monitor {ts}",
            "Prueba de disparo G3. Cerrar este issue.\n\n"
            "No indica un fallo real del scraper.",
            prueba=True,
        )
        return

    if status == "cancelled":
        print("Corrida cancelada: sin alerta.", flush=True)
        return

    motivos: list[str] = []
    if status == "failure":
        motivos.append("La corrida de GitHub Actions terminó en **failure** (scraper, refresh, embed u otro step).")
    if log.get("alerta_anomala") == "1":
        motivos.append(
            "Ingesta marcó **alerta_anomala=1** (API HTTP ≠ 200, totalElements=0, o sin registros en corrida completa)."
        )

    if not motivos:
        print(
            f"Sin alerta. job-status={status or '-'} "
            f"nuevos={log.get('nuevos_esta_corrida', '-')} "
            f"alerta_anomala={log.get('alerta_anomala', '-')}",
            flush=True,
        )
        return

    cuerpo = (
        f"Corrida SEACE Monitor — {ts}\n\n"
        + "\n".join(f"- {m}" for m in motivos)
        + "\n\n"
        f"Log `data/ultima_ingesta.txt`:\n```\n"
        + (LOG.read_text(encoding="utf-8") if LOG.exists() else "(no hay log)")
        + "\n```\n"
    )
    if run_url:
        cuerpo += f"\nRun: {run_url}\n"
    _publicar(f"[G3] Corrida SEACE falló o anómala — {ts}", cuerpo, prueba=False)


if __name__ == "__main__":
    main()
