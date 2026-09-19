-- =====================================================================
-- Backfill de proceso_evento desde chunks_tdr (una sola vez, idempotente)
-- =====================================================================
-- proceso_evento se empezó a instrumentar el 19 sep; los chunks históricos
-- ya existían en chunks_tdr sin evento. Este backfill deriva, por contrato:
--   * evento 'chunked'   (contratos con fuente=pdf)  → n_chunks_pdf + chars
--   * evento 'embedded'  (contratos con embedding_v2) → chunks pdf/api +
--     tokens_est + costo_usd prorrateado (chars/4 × EMBED_USD_PER_M 0.0375)
--
-- Marcados con detalle.backfill = true y run_id = 'backfill' para distinguirlos
-- de los eventos reales que escribirá el pipeline de ahora en adelante.
-- created_at se toma del max(created_at) de los chunks (última vez procesado).
--
-- Idempotente: NOT EXISTS evita duplicar contratos que ya tengan el evento.
-- Ejecutar: uv run python scripts/run_sql.py backfill_proceso_evento.sql
-- =====================================================================

-- 1) chunked (solo donde no exista aún)
INSERT INTO proceso_evento
  (contrato_id, etapa, n_chunks_pdf, chars_tdr, chunk_version, run_id, detalle, created_at)
SELECT
  ct.contrato_id,
  'chunked',
  count(*)::int,
  coalesce(sum(length(coalesce(ct.chunk_embed_text, ''))), 0),
  coalesce(c.chunk_version, '500_0'),
  'backfill',
  jsonb_build_object('backfill', true, 'origen', 'chunks_tdr.fuente=pdf'),
  max(ct.created_at)
FROM chunks_tdr ct
JOIN contratos c ON c.id = ct.contrato_id
WHERE ct.fuente = 'pdf'
  AND NOT EXISTS (
    SELECT 1 FROM proceso_evento pe
    WHERE pe.contrato_id = ct.contrato_id AND pe.etapa = 'chunked'
  )
GROUP BY ct.contrato_id, c.chunk_version;

-- 2) embedded (solo donde no exista aún)
INSERT INTO proceso_evento
  (contrato_id, etapa, n_chunks_pdf, n_chunks_api, chars_tdr, tokens_est,
   costo_usd, chunk_version, run_id, detalle, created_at)
SELECT
  ct.contrato_id,
  'embedded',
  count(*) FILTER (WHERE ct.fuente = 'pdf')::int,
  count(*) FILTER (WHERE ct.fuente = 'api')::int,
  coalesce(sum(length(coalesce(ct.chunk_embed_text, ''))), 0),
  (coalesce(sum(length(coalesce(ct.chunk_embed_text, ''))), 0) / 4)::int,
  round(
    (coalesce(sum(length(coalesce(ct.chunk_embed_text, ''))), 0) / 4.0)
    / 1000000.0 * 0.0375, 8),
  coalesce(c.chunk_version, '500_0'),
  'backfill',
  jsonb_build_object('backfill', true, 'origen', 'chunks_tdr.embedding_v2'),
  max(ct.created_at)
FROM chunks_tdr ct
JOIN contratos c ON c.id = ct.contrato_id
WHERE ct.embedding_v2 IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM proceso_evento pe
    WHERE pe.contrato_id = ct.contrato_id AND pe.etapa = 'embedded'
  )
GROUP BY ct.contrato_id, c.chunk_version;
