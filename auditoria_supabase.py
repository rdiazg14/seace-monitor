#!/usr/bin/env python3
"""
Auditoría de datos en Supabase — Fase de validación completa.
Ejecutar: uv run python auditoria_supabase.py
"""
from __future__ import annotations
import os
from pathlib import Path
from supabase import create_client

_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise SystemExit("ERROR: .env sin SUPABASE_URL / SUPABASE_SERVICE_KEY")

supa = create_client(SUPABASE_URL, SUPABASE_KEY)

def q(description, fn):
    try:
        result = fn()
        print(f"  {'✓':<3} {description:<45} {result}")
        return result
    except Exception as e:
        print(f"  {'✗':<3} {description:<45} ERROR: {e}")
        return None

print("=" * 65)
print("AUDITORÍA SUPABASE — SEACE Monitor")
print("=" * 65)

print("\n── Tabla: contratos ─────────────────────────────────────────")
total = q("Total contratos",
    lambda: supa.table("contratos").select("id", count="exact").execute().count)
vigentes = q("Estado = Vigente",
    lambda: supa.table("contratos").select("id", count="exact").eq("estado", "Vigente").execute().count)
con_detalle = q("detalle_cargado = true",
    lambda: supa.table("contratos").select("id", count="exact").eq("detalle_cargado", True).execute().count)

# items_json no vacío (check via negating null)
con_items_res = supa.table("contratos").select("id", count="exact").not_.is_("items_json", "null").execute()
con_items = q("items_json IS NOT NULL",
    lambda: con_items_res.count)

print("\n── Tabla: chunks_tdr ────────────────────────────────────────")
total_chunks = q("Total chunks",
    lambda: supa.table("chunks_tdr").select("id", count="exact").execute().count)
con_emb_res = supa.table("chunks_tdr").select("id", count="exact").not_.is_("embedding", "null").execute()
con_emb = q("embedding IS NOT NULL",
    lambda: con_emb_res.count)

# Distribución por tipo
print("\n── Distribución chunks por tipo ─────────────────────────────")
try:
    tipos_res = supa.table("chunks_tdr").select("tipo").execute()
    from collections import Counter
    c = Counter(r["tipo"] for r in tipos_res.data)
    for tipo, n in sorted(c.items(), key=lambda x: -x[1]):
        print(f"  {'':3} {tipo:<45} {n}")
except Exception as e:
    print(f"  ✗   Error distribución: {e}")

print("\n── Consistencia ─────────────────────────────────────────────")
sin_detalle_res = supa.table("contratos").select("id", count="exact").eq("estado","Vigente").eq("detalle_cargado", False).execute()
sin_detalle = q("Vigentes sin detalle (esperado: 0)",
    lambda: sin_detalle_res.count)

sin_emb_res = supa.table("chunks_tdr").select("id", count="exact").is_("embedding", "null").execute()
sin_emb = q("Chunks sin embedding (esperado: 0)",
    lambda: sin_emb_res.count)

print("\n── Alertas ──────────────────────────────────────────────────")
alertas = []
if total and vigentes and vigentes > 0:
    ratio = con_detalle / vigentes if (con_detalle and vigentes) else 0
    if ratio < 0.9:
        alertas.append(f"⚠ Solo {ratio:.0%} de vigentes tienen detalle cargado")
if sin_detalle and sin_detalle > 0:
    alertas.append(f"⚠ {sin_detalle} vigentes sin detalle — ejecutar enriquecer_detalle.py")
if sin_emb and sin_emb > 0:
    alertas.append(f"⚠ {sin_emb} chunks sin embedding — ejecutar generar_embeddings.py")
if total_chunks is not None and total_chunks == 0:
    alertas.append("🔴 CRÍTICO: 0 chunks — el RAG no puede funcionar")

if alertas:
    for a in alertas:
        print(f"  {a}")
else:
    print("  ✓ Sin alertas — datos consistentes")

print("\n" + "=" * 65)
print("FIN DE AUDITORÍA SUPABASE")
print("=" * 65)
