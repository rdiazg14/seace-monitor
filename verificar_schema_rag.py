#!/usr/bin/env python3
"""
Fase 1.3 — Verifica que el schema RAG se creó correctamente en Supabase.
Ejecutar con: uv run python verificar_schema_rag.py
"""
import os
from pathlib import Path
from supabase import create_client

# Carga .env manualmente (sin dependencia extra)
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise SystemExit("ERROR: SUPABASE_URL o SUPABASE_SERVICE_KEY no encontrados en .env")

supa = create_client(SUPABASE_URL, SUPABASE_KEY)

print("=" * 60)
print("Verificando schema RAG en Supabase...")
print("=" * 60)

# 1. Verificar columnas nuevas en contratos
res = supa.rpc("buscar_tdr", {
    "query_embedding": [0.0] * 768,
    "match_count": 1,
}).execute()
funcion_ok = True  # Si llegamos aquí sin excepción, existe

# 2. Verificar tabla chunks_tdr existe (intentar select vacío)
try:
    res2 = supa.table("chunks_tdr").select("id").limit(1).execute()
    tabla_ok = True
except Exception as e:
    tabla_ok = False
    print(f"  [ERROR] tabla chunks_tdr: {e}")

# 3. Verificar columnas en contratos
try:
    res3 = supa.table("contratos").select(
        "id, nom_area_usuaria, items_json, detalle_cargado"
    ).limit(1).execute()
    columnas_ok = True
except Exception as e:
    columnas_ok = False
    print(f"  [ERROR] columnas contratos: {e}")

print(f"\n  pgvector + función buscar_tdr : {'✓ OK' if funcion_ok else '✗ FALTA'}")
print(f"  tabla chunks_tdr             : {'✓ OK' if tabla_ok else '✗ FALTA'}")
print(f"  columnas en contratos        : {'✓ OK' if columnas_ok else '✗ FALTA'}")

total = sum([funcion_ok, tabla_ok, columnas_ok])
print(f"\n{'✓ TODO LISTO — Fase 1 completada' if total == 3 else f'✗ {3 - total} item(s) pendiente(s)'}")
print("=" * 60)
