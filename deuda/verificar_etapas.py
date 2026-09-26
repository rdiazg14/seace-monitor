#!/usr/bin/env python3
"""
Verifica la cobertura de etapas_json en el universo de Ruta del día.

Reporta cuántos contratos IT/IA (Vigente + En Evaluación) tienen cronograma
de etapas y muestra una muestra de vigentes con ventana de consultas para
confirmar que la detección funciona.

Uso: uv run python deuda/verificar_etapas.py
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg

_env = Path(__file__).resolve().parent.parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

dsn = os.environ.get("DATABASE_URL", "")
c = psycopg.connect(dsn, connect_timeout=30, sslmode="require")
cur = c.cursor()

cur.execute(
    """
    SELECT
      count(*) AS total,
      count(*) FILTER (WHERE etapas_json IS NOT NULL) AS con_etapas,
      count(*) FILTER (WHERE etapas_json IS NULL) AS sin_etapas,
      count(*) FILTER (WHERE estado = 'Vigente') AS vigentes,
      count(*) FILTER (WHERE estado = 'Vigente' AND etapas_json IS NOT NULL) AS vigentes_con_etapas
    FROM v_contratos
    WHERE estado IN ('Vigente','En Evaluación')
      AND (categoria_it IS NOT NULL OR relevancia_ia IS NOT NULL)
    """
)
total, con_etapas, sin_etapas, vigentes, vigentes_con = cur.fetchone()
print(f"Universo Ruta del día (IT/IA):")
print(f"  total              : {total}")
print(f"  con etapas_json    : {con_etapas}")
print(f"  sin etapas_json    : {sin_etapas}")
print(f"  vigentes           : {vigentes} (con etapas: {vigentes_con})")

# Muestra: vigentes cuya etapa de consultas está abierta ahora.
cur.execute(
    """
    SELECT id, descripcion_contrato, etapas_json
    FROM v_contratos
    WHERE estado = 'Vigente'
      AND etapas_json IS NOT NULL
      AND (categoria_it IS NOT NULL OR relevancia_ia IS NOT NULL)
    ORDER BY fecha_fin_cotizacion ASC NULLS LAST
    LIMIT 5
    """
)
print("\nMuestra de vigentes (cierre más próximo):")
for cid, desc, etapas in cur.fetchall():
    consulta = next((e for e in (etapas or []) if "consulta" in (e.get("etapa") or "").lower()
                     or "absolucion" in (e.get("etapa") or "").lower()), None)
    print(f"  id={cid} {desc or ''}")
    if consulta:
        print(f"      consultas: {consulta.get('fec_ini')} -> {consulta.get('fec_fin')}")
    else:
        print(f"      etapas: {[e.get('etapa') for e in (etapas or [])]}")

c.close()
