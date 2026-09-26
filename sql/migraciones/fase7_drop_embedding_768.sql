-- =====================================================================
-- FASE 7 — Retiro del bundle v1 de embeddings (BGE 768)
-- =====================================================================
-- Elimina el embedding legacy `embedding vector(768)` (bge-base-en-v1.5),
-- su índice ivfflat y la función `buscar_tdr(768)`. El chat usa Gemini:
-- `embedding_v2 vector(1536)` + `buscar_tdr_v2` + HNSW. Nada en producción
-- escribe ni lee `embedding(768)` desde hace semanas (pipeline.yml solo
-- alimenta embedding_v2; el proxy usa RAG_BACKEND=v2).
--
-- Guard: aborta si quedara algún chunk con embedding(768) y SIN embedding_v2
-- (evita perder vector por accidente). El dato es derivado y regenerable con
-- Gemini, pero el guard da margen de seguridad.
--
-- Smoke test incluido al final (buscar_tdr_v2 debe seguir respondiendo).
-- Idempotente: DROP ... IF EXISTS.
--
-- Ejecutar una sola vez en Supabase → SQL Editor (o vía psql/run_sql.py).
-- =====================================================================

BEGIN;

-- ── 1. Guard: embedding(768) huérfano (sin v2) ──────────────────────────
DO $$
DECLARE
  orphan INT;
  total_768 INT;
  total_v2 INT;
BEGIN
  SELECT count(*) INTO orphan
    FROM chunks_tdr
   WHERE embedding IS NOT NULL AND embedding_v2 IS NULL;
  SELECT count(*) INTO total_768 FROM chunks_tdr WHERE embedding IS NOT NULL;
  SELECT count(*) INTO total_v2  FROM chunks_tdr WHERE embedding_v2 IS NOT NULL;

  IF orphan > 0 THEN
    RAISE EXCEPTION 'FASE7 ABORTADA: embedding(768) tiene % chunks sin embedding_v2', orphan;
  END IF;

  RAISE NOTICE 'FASE7 pre-check OK: embedding(768)=%  embedding_v2=%  huérfanos=0', total_768, total_v2;
END $$;

-- ── 2. Drop índice ivfflat (768) ────────────────────────────────────────
DROP INDEX IF EXISTS idx_chunks_embedding;

-- ── 3. Drop funciones buscar_tdr(768) — ambas firmas ────────────────────
DROP FUNCTION IF EXISTS public.buscar_tdr(vector, integer, text);
DROP FUNCTION IF EXISTS public.buscar_tdr(vector, integer, text, double precision);

-- ── 4. Drop columna embedding(768) ──────────────────────────────────────
ALTER TABLE chunks_tdr DROP COLUMN IF EXISTS embedding;

COMMIT;

-- =====================================================================
-- Smoke test (fuera de la transacción): la búsqueda v2 sigue viva.
-- =====================================================================
SELECT
  (SELECT count(*) FROM chunks_tdr WHERE embedding_v2 IS NOT NULL) AS chunks_v2,

  -- vector cero no es semántico → debe devolver 0 hits (no error)
  (SELECT count(*) FROM buscar_tdr_v2(
      array_fill(0.0, ARRAY[1536])::vector(1536), 5, 'Vigente', 0.20
  )) AS hits_cero_v2,

  -- confirmar que ya no existen las piezas v1
  (SELECT count(*) FROM information_schema.columns
     WHERE table_schema='public' AND table_name='chunks_tdr' AND column_name='embedding'
  ) AS col_embedding_768_restante,

  (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
     WHERE n.nspname='public' AND p.proname='buscar_tdr'
  ) AS fn_buscar_tdr_768_restante;
