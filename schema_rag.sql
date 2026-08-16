-- ============================================================
-- FASE 1.2 — Esquema RAG para SEACE Monitor
-- Ejecutar en Supabase SQL Editor (una sola vez)
-- ============================================================

-- 1. Extensión pgvector
-- Habilita el tipo vector y operadores de similitud coseno
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. Nuevas columnas en la tabla contratos
--    (datos extra que vienen de la API de detalle)
ALTER TABLE contratos
  ADD COLUMN IF NOT EXISTS nom_area_usuaria  TEXT,
  ADD COLUMN IF NOT EXISTS items_json        JSONB,
  ADD COLUMN IF NOT EXISTS detalle_cargado   BOOLEAN NOT NULL DEFAULT FALSE;

-- 3. Tabla chunks_tdr
--    Almacena fragmentos de texto + embedding vectorial
--    Un contrato puede tener varios chunks (descripción, por ítem, etc.)
CREATE TABLE IF NOT EXISTS chunks_tdr (
  id            BIGSERIAL PRIMARY KEY,
  contrato_id   BIGINT     NOT NULL REFERENCES contratos(id) ON DELETE CASCADE,
  chunk_index   SMALLINT   NOT NULL,
  tipo          TEXT       NOT NULL,  -- 'descripcion' | 'item' | 'area_item'
  texto         TEXT       NOT NULL,
  embedding     vector(768),          -- bge-base-en-v1.5 → 768 dims
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (contrato_id, chunk_index)
);

-- 4. Índice ANN para búsqueda semántica rápida (coseno)
--    lists=100 recomendado para ~50k–500k filas
CREATE INDEX IF NOT EXISTS idx_chunks_embedding
  ON chunks_tdr
  USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 100);

-- Índice auxiliar para filtrar por contrato_id
CREATE INDEX IF NOT EXISTS idx_chunks_contrato
  ON chunks_tdr (contrato_id);

-- 5. RLS — anon puede leer, solo service_role puede escribir
ALTER TABLE chunks_tdr ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "chunks_tdr_select_anon" ON chunks_tdr;
CREATE POLICY "chunks_tdr_select_anon"
  ON chunks_tdr FOR SELECT USING (true);

-- 6. Función buscar_tdr(query_embedding, match_count, filter_estado)
--    Búsqueda semántica con filtro opcional por estado del contrato
--    Retorna: id_contrato, chunk_index, tipo, texto, similarity score
CREATE OR REPLACE FUNCTION buscar_tdr(
  query_embedding vector(768),
  match_count     INT     DEFAULT 10,
  filter_estado   TEXT    DEFAULT NULL,
  min_similarity  FLOAT   DEFAULT 0.80
)
RETURNS TABLE (
  contrato_id   BIGINT,
  chunk_index   SMALLINT,
  tipo          TEXT,
  texto         TEXT,
  similarity    FLOAT
)
LANGUAGE SQL STABLE AS $$
  SELECT
    ct.contrato_id,
    ct.chunk_index,
    ct.tipo,
    ct.texto,
    (1 - (ct.embedding <=> query_embedding))::FLOAT AS similarity
  FROM chunks_tdr ct
  JOIN contratos  c ON c.id = ct.contrato_id
  WHERE ct.embedding IS NOT NULL
    AND (filter_estado IS NULL OR c.estado = filter_estado)
    AND (1 - (ct.embedding <=> query_embedding)) > min_similarity
  ORDER BY ct.embedding <=> query_embedding
  LIMIT match_count;
$$;

-- 7. Verificación rápida — ejecuta esto al final para confirmar
SELECT
  EXISTS (SELECT 1 FROM pg_extension   WHERE extname = 'vector')              AS pgvector_ok,
  EXISTS (SELECT 1 FROM pg_tables      WHERE tablename = 'chunks_tdr')        AS tabla_ok,
  EXISTS (SELECT 1 FROM pg_indexes     WHERE indexname = 'idx_chunks_embedding') AS indice_ok,
  EXISTS (SELECT 1 FROM pg_proc        WHERE proname = 'buscar_tdr')          AS funcion_ok,
  (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_name = 'contratos'
       AND column_name IN ('nom_area_usuaria','items_json','detalle_cargado')
  ) = 3                                                                        AS columnas_ok;
