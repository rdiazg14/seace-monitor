-- ============================================================
-- FIX: umbral de similitud en buscar_tdr
-- Ejecutar en Supabase → SQL Editor
-- ============================================================

CREATE OR REPLACE FUNCTION buscar_tdr(
  query_embedding VECTOR(768),
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
LANGUAGE SQL
STABLE
AS $$
  SELECT
    ct.contrato_id,
    ct.chunk_index,
    ct.tipo,
    ct.texto,
    (1 - (ct.embedding <=> query_embedding))::FLOAT AS similarity
  FROM chunks_tdr ct
  JOIN contratos c ON c.id = ct.contrato_id
  WHERE ct.embedding IS NOT NULL
    AND (filter_estado IS NULL OR c.estado = filter_estado)
    AND (1 - (ct.embedding <=> query_embedding)) > min_similarity
  ORDER BY ct.embedding <=> query_embedding
  LIMIT match_count;
$$;

-- Verificación: debe devolver 0 filas (el vector cero no es semántico)
SELECT COUNT(*) AS hits_cero
FROM buscar_tdr(array_fill(0.0, ARRAY[768])::vector(768), 5, NULL, 0.80);
