-- =====================================================================
-- proceso_evento  ·  seguimiento estadístico por contrato (event log)
-- =====================================================================
-- Tabla append-only de hitos del procesamiento de cada contrato. Cada vez
-- que un paso procesa un contrato (extraer TDR, chunkear, embeder, migrar
-- chunking, contenedor) inserta una fila con su timestamp y sus stats:
-- cuántos chunks generó, cuántos chars/tokens, cuánto costó el embedding.
--
-- Da respuesta a "este PDF cuántos chunks generó, cuánto costó, a qué hora,
-- cuántos embeddings v2 tiene" con trazabilidad completa (nunca se pisa).
--
-- Quién escribe: pipeline seace-monitor (psycopg directo / supabase-py
-- service_role, ambos bypass RLS). Quién lee: admin (es_admin).
-- =====================================================================

CREATE TABLE IF NOT EXISTS public.proceso_evento (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now(),
  contrato_id bigint NOT NULL REFERENCES contratos(id) ON DELETE CASCADE,
  -- etapa: tdr_extraido | contenedor | chunked | embedded | migrado_300_60
  etapa text NOT NULL,
  n_chunks_pdf integer,
  n_chunks_api integer,
  chars_tdr integer,
  tokens_est integer,
  costo_usd numeric(14,6),
  tipo_extraccion text,
  chunk_version text,
  run_id text,
  detalle jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_proceso_evento_contrato
  ON public.proceso_evento (contrato_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_proceso_evento_etapa_ts
  ON public.proceso_evento (etapa, created_at DESC);

ALTER TABLE public.proceso_evento ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.proceso_evento FROM anon, authenticated;
GRANT SELECT ON TABLE public.proceso_evento TO authenticated;

DROP POLICY IF EXISTS proceso_evento_select_admin ON public.proceso_evento;
CREATE POLICY proceso_evento_select_admin
  ON public.proceso_evento FOR SELECT TO authenticated
  USING (public.es_admin());

COMMENT ON TABLE public.proceso_evento IS
  'Event log append-only del procesamiento por contrato: extracción/chunking/embedding/migración con stats (chunks, chars, tokens, costo) y timestamp.';
COMMENT ON COLUMN public.proceso_evento.etapa IS
  'tdr_extraido | contenedor | chunked | embedded | migrado_300_60';
COMMENT ON COLUMN public.proceso_evento.costo_usd IS
  'Costo estimado USD del embedding (prorrateado por chars del contrato).';
