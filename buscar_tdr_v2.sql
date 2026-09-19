-- =====================================================================
-- buscar_tdr_v2  ·  SEACE Monitor v2  ·  PARALELO a buscar_tdr(768)
-- =====================================================================
-- NO toca buscar_tdr ni embedding(768) ni el ivfflat.
-- Usa idx_chunks_embedding_v2_hnsw sobre embedding_v2 vector(1536).
-- Ejecutar en: Supabase → SQL Editor.
--
-- 19 sep: se fuerza hnsw.ef_search=200 (default 40 perdía recall: ~145 vs
-- 190 relevantes en el top-20). set_config(..., true) es local a la
-- transacción (PostgREST envuelve el RPC en una), así que es determinista
-- incluso con el pooler Supavisor (que reutiliza backends).
-- =====================================================================

CREATE OR REPLACE FUNCTION buscar_tdr_v2(
  query_embedding VECTOR(1536),
  match_count     INT     DEFAULT 10,
  filter_estado   TEXT    DEFAULT 'Vigente',
  min_similarity  FLOAT   DEFAULT 0.20
)
RETURNS TABLE (
  contrato_id   BIGINT,
  chunk_index   SMALLINT,
  tipo          TEXT,
  texto         TEXT,
  similarity    FLOAT,
  fuente        TEXT
)
LANGUAGE plpgsql
AS $$
BEGIN
  PERFORM set_config('hnsw.ef_search', '200', true);
  RETURN QUERY
    SELECT
      ct.contrato_id,
      ct.chunk_index,
      ct.tipo,
      ct.texto,
      (1 - (ct.embedding_v2 <=> query_embedding))::FLOAT AS similarity,
      ct.fuente
    FROM chunks_tdr ct
    JOIN contratos c ON c.id = ct.contrato_id
    WHERE ct.embedding_v2 IS NOT NULL
      AND (filter_estado IS NULL OR c.estado = filter_estado)
      AND (1 - (ct.embedding_v2 <=> query_embedding)) > min_similarity
    ORDER BY ct.embedding_v2 <=> query_embedding
    LIMIT match_count;
END;
$$;

GRANT EXECUTE ON FUNCTION buscar_tdr_v2(VECTOR, INT, TEXT, FLOAT)
  TO anon, authenticated, service_role;

NOTIFY pgrst, 'reload schema';

-- Verificación: 0 hits (vector cero no es semántico)
SELECT COUNT(*) AS hits_cero
FROM buscar_tdr_v2(array_fill(0.0, ARRAY[1536])::vector(1536), 5, 'Vigente', 0.20);
