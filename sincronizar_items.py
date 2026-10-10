#!/usr/bin/env python3
"""
Sincroniza contrato_items desde contratos.items_json (idempotente e incremental).

contrato_items es el espejo en filas de items_json: permite cruzar los ítems
con cubso_catalogo y analizarlos por código. La fuente de verdad sigue siendo
items_json.

Antes este paso borraba toda la tabla y la volvía a llenar en cada corrida
(millones de filas reescritas sin que nada hubiera cambiado). Ahora (DATA-002):
  1. Calcula en una tabla temporal las filas que debería haber, con el MISMO
     mapeo de la fase 3 (contrato_id, item_nro 1-based, cod_cubso, nom_cubso,
     descripcion, cantidad, unidad, distrito).
  2. Compara por contrato la huella de sus filas actuales con la de las
     deseadas.
  3. Borra y reinserta SOLO los contratos cuya huella difiere (ítems nuevos,
     cambiados, o contratos que ya no tienen ítems).

El resultado final es idéntico al de la reconstrucción completa. Idempotente:
una segunda corrida sin cambios no escribe ninguna fila. Atómico: una sola
transacción. Además toma un pg_try_advisory_xact_lock: pipeline.yml y
deteccion_temprana.yml lo ejecutan en horarios que pueden solaparse; si otra
corrida está en curso, esta se salta (FIX-011).
Pensado para correr como paso `continue-on-error` tras enriquecer_detalle.py;
si falla, items_json ya quedó guardado y el pipeline no se cae.

Uso:
  python sincronizar_items.py
"""
from __future__ import annotations

import os
import time

from seace_monitor.config import cargar_env
from seace_monitor.db import connect

cargar_env()

# 726001 = clave fija del paso: una sola sincronización a la vez.
SQL_LOCK = "SELECT pg_try_advisory_xact_lock(726001)"

# Filas deseadas. Mismo mapeo que docs/sql/migraciones/capas_fase3_items.sql.
SQL_DESEADO = """
    CREATE TEMP TABLE _items_deseados ON COMMIT DROP AS
    SELECT
        c.id AS contrato_id,
        ord.ordinality::int AS item_nro,
        nullif(ord.elem->>'cod_cubso', '') AS cod_cubso,
        nullif(ord.elem->>'nom_cubso', '') AS nom_cubso,
        nullif(ord.elem->>'descripcion', '') AS descripcion,
        CASE
          WHEN ord.elem->>'cantidad' IS NULL OR ord.elem->>'cantidad' = ''
            THEN NULL
          ELSE (ord.elem->>'cantidad')::numeric
        END AS cantidad,
        nullif(ord.elem->>'unidad', '') AS unidad,
        nullif(ord.elem->>'distrito', '') AS distrito
    FROM contratos c
    -- El CASE va dentro de la función: un WHERE no garantiza que Postgres
    -- filtre antes de expandir, y un items_json que no sea arreglo abortaba
    -- toda la sincronización.
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(c.items_json) = 'array' THEN c.items_json ELSE '[]'::jsonb END
    ) WITH ORDINALITY AS ord(elem, ordinality)
"""

# Huella por contrato de sus filas (todas las columnas, en orden de ítem). Un
# contrato presente en un solo lado tiene huella NULL en el otro y cuenta como
# cambiado. La comparación exacta evita depender de fechas de modificación.
_HUELLA = """
    SELECT contrato_id,
           md5(jsonb_agg(jsonb_build_array(item_nro, cod_cubso, nom_cubso, descripcion,
                                           cantidad, unidad, distrito)
                         ORDER BY item_nro)::text) AS huella
    FROM {tabla}
    GROUP BY contrato_id
"""

SQL_CAMBIADOS = f"""
    CREATE TEMP TABLE _items_cambiados ON COMMIT DROP AS
    SELECT coalesce(d.contrato_id, a.contrato_id) AS contrato_id
    FROM ({_HUELLA.format(tabla="_items_deseados")}) d
    FULL JOIN ({_HUELLA.format(tabla="contrato_items")}) a USING (contrato_id)
    WHERE d.huella IS DISTINCT FROM a.huella
"""

SQL_DELETE = """
    DELETE FROM contrato_items ci
    USING _items_cambiados x
    WHERE ci.contrato_id = x.contrato_id
"""

SQL_INSERT = """
    INSERT INTO contrato_items (
        contrato_id, item_nro, cod_cubso, nom_cubso, descripcion,
        cantidad, unidad, distrito
    )
    SELECT d.contrato_id, d.item_nro, d.cod_cubso, d.nom_cubso, d.descripcion,
           d.cantidad, d.unidad, d.distrito
    FROM _items_deseados d
    JOIN _items_cambiados x USING (contrato_id)
"""

SQL_DESYNC = """
    SELECT count(*)::int
    FROM contratos c
    WHERE jsonb_array_length(
            CASE WHEN jsonb_typeof(c.items_json) = 'array' THEN c.items_json ELSE '[]'::jsonb END
          ) > 0
      AND NOT EXISTS (
        SELECT 1 FROM contrato_items ci WHERE ci.contrato_id = c.id
      )
"""


def sincronizar(conn) -> dict | None:
    """Aplica la sincronización en una transacción. None si otra corrida tiene el lock."""
    with conn.transaction():
        if not conn.execute(SQL_LOCK).fetchone()[0]:
            return None
        conn.execute(SQL_DESEADO)
        conn.execute(SQL_CAMBIADOS)
        cambiados = conn.execute("SELECT count(*)::int FROM _items_cambiados").fetchone()[0]
        n_del = conn.execute(SQL_DELETE).rowcount
        n_ins = conn.execute(SQL_INSERT).rowcount
    return {"contratos_cambiados": cambiados, "borrados": n_del, "insertados": n_ins}


def main() -> int:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("ERROR: falta DATABASE_URL", flush=True)
        return 2

    t0 = time.perf_counter()
    with connect(dsn) as conn:
        r = sincronizar(conn)
        if r is None:
            print("[items] otra sincronización en curso; se omite (mismo resultado)", flush=True)
            return 0
        filas = conn.execute("SELECT count(*)::int FROM contrato_items").fetchone()[0]
        ctrs = conn.execute("SELECT count(DISTINCT contrato_id)::int FROM contrato_items").fetchone()[0]
        desync = conn.execute(SQL_DESYNC).fetchone()[0]
    dt = time.perf_counter() - t0

    print(
        f"[items] contratos_cambiados={r['contratos_cambiados']} borrados={r['borrados']} "
        f"insertados={r['insertados']} filas={filas} contratos={ctrs} "
        f"desync_restante={desync} en {dt:.2f}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
