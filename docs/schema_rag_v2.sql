-- =====================================================================
-- schema_rag_v2.sql  ·  SEACE Monitor v2  ·  FASE 1 (expand) — FINAL
-- =====================================================================
-- Nombres reales confirmados: FK = contrato_id · estados = 'Vigente',
-- 'En Evaluación', 'Culminado' · FTS = texto_busqueda (GIN ya existe).
--
-- ADITIVO e IDEMPOTENTE. No dropea nada. La columna `embedding(768)` y
-- su ivfflat siguen vivos hasta la FASE 7. El RAG actual sigue funcionando.
--
-- CÓMO EJECUTAR:
--   1) Corre el BLOQUE A completo (una sola vez, de arriba a abajo).
--   2) Corre el BLOQUE B para verificar que quedó todo.
-- Ejecutar en: Supabase → SQL Editor.
-- =====================================================================


-- #####################################################################
-- BLOQUE A — MIGRACIÓN (corre todo esto de una vez)
-- #####################################################################

-- 1) Extensión (no-op si ya existe)
CREATE EXTENSION IF NOT EXISTS vector;


-- 2) contratos: tracking del PDF + frescura de estado
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS req_url               TEXT;
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS pdf_descargado        BOOLEAN     DEFAULT FALSE;
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS pdf_procesado         BOOLEAN     DEFAULT FALSE;
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS pdf_es_imagen         BOOLEAN;
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS tdr_texto             TEXT;
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS pdf_hash              TEXT;
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS estado_verificado_at  TIMESTAMPTZ;

COMMENT ON COLUMN contratos.req_url              IS 'URL de descarga del requerimiento (TDR).';
COMMENT ON COLUMN contratos.pdf_es_imagen        IS 'TRUE si PyMuPDF no extrajo texto y se uso OCR (PDF escaneado).';
COMMENT ON COLUMN contratos.estado_verificado_at IS 'Ultimo refresco de estado (FASE G1 / frescura).';


-- 3) chunks_tdr: embeddings nuevos EN PARALELO (la vieja `embedding` no se toca)
ALTER TABLE chunks_tdr ADD COLUMN IF NOT EXISTS fuente        TEXT DEFAULT 'api';   -- 'api' | 'pdf'
ALTER TABLE chunks_tdr ADD COLUMN IF NOT EXISTS embedding_v2  vector(1536);         -- gemini-embedding-001 @1536

COMMENT ON COLUMN chunks_tdr.fuente       IS 'Origen del chunk: api o pdf.';
COMMENT ON COLUMN chunks_tdr.embedding_v2 IS 'gemini-embedding-001, 1536 dims. Reemplaza a embedding(768) en FASE 7.';


-- 4) Índices

-- 4.1 Vectorial HNSW sobre la nueva columna (reemplaza al ivfflat)
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_v2_hnsw
  ON chunks_tdr USING hnsw (embedding_v2 vector_cosine_ops);

-- 4.2 FTS lexico (ya existe como idx_contratos_fts; se deja por idempotencia)
CREATE INDEX IF NOT EXISTS idx_contratos_fts
  ON contratos USING gin (texto_busqueda);

-- 4.3 Descarga de PDF (FASE 3): WHERE estado='Vigente' AND pdf_descargado=false
CREATE INDEX IF NOT EXISTS idx_contratos_pdf_pendiente
  ON contratos (estado)
  WHERE pdf_descargado = FALSE;

-- 4.4 Frescura (FASE G1): re-consulta no-terminales, prioriza los mas viejos sin verificar
CREATE INDEX IF NOT EXISTS idx_contratos_estado_verif
  ON contratos (estado_verificado_at NULLS FIRST)
  WHERE estado IN ('Vigente', 'En Evaluación');

-- 4.5 Garbage-collection (FASE G1): DELETE FROM chunks_tdr WHERE contrato_id = :id
CREATE INDEX IF NOT EXISTS idx_chunks_contrato_id
  ON chunks_tdr (contrato_id);

-- 4.6 Filtro por fuente en el retrieval hibrido
CREATE INDEX IF NOT EXISTS idx_chunks_fuente
  ON chunks_tdr (fuente);

-- FIN DEL BLOQUE A
-- #####################################################################


-- #####################################################################
-- BLOQUE B — VERIFICACIÓN (corre esto DESPUÉS; solo lee, no modifica)
-- #####################################################################

-- B.1 Debe devolver 9 filas (7 de contratos + 2 de chunks_tdr)
SELECT table_name, column_name
FROM information_schema.columns
WHERE (table_name = 'contratos'  AND column_name IN
         ('req_url','pdf_descargado','pdf_procesado','pdf_es_imagen',
          'tdr_texto','pdf_hash','estado_verificado_at'))
   OR (table_name = 'chunks_tdr' AND column_name IN ('fuente','embedding_v2'))
ORDER BY table_name, column_name;

-- B.2 Debe listar los 6 indices nuevos/relevantes
SELECT indexname
FROM pg_indexes
WHERE indexname IN (
  'idx_chunks_embedding_v2_hnsw',
  'idx_contratos_fts',
  'idx_contratos_pdf_pendiente',
  'idx_contratos_estado_verif',
  'idx_chunks_contrato_id',
  'idx_chunks_fuente'
)
ORDER BY indexname;

-- B.3 Sanity: los chunks existentes deben quedar todos con fuente='api'
--     y embedding_v2 en NULL (se poblara en la FASE 5).
SELECT
  COUNT(*)                                     AS total_chunks,
  COUNT(*) FILTER (WHERE fuente = 'api')        AS fuente_api,
  COUNT(*) FILTER (WHERE embedding_v2 IS NULL)  AS sin_embedding_v2
FROM chunks_tdr;

-- FIN DEL BLOQUE B
-- #####################################################################
