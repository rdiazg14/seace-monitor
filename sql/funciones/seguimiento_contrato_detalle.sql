-- =====================================================================
-- Seguimiento de proceso — agregados y timeline (nivel especialista)
-- =====================================================================
-- Complementa fn_seguimiento_contrato (tabla maestra) con dos piezas:
--   1. fn_proceso_evento_resumen()  → KPIs globales del pipeline de IA/embedding
--   2. fn_proceso_eventos(id)       → timeline completo de eventos de un contrato
-- Ambos JSONB vía RPC, guard es_admin(). Idempotente.
-- =====================================================================

-- ─────────────────────────────────────────────────────────────────────
-- 1) Resumen agregado (KPIs) del seguimiento
-- ─────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.fn_proceso_evento_resumen()
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v jsonb;
BEGIN
  IF NOT public.es_admin() THEN
    RAISE EXCEPTION 'admin_required' USING ERRCODE = '42501';
  END IF;

  SELECT jsonb_build_object(
    -- Contratos trazados y volumen de eventos
    'contratos_con_eventos', (SELECT count(DISTINCT contrato_id)::int FROM proceso_evento),
    'eventos_total', (SELECT count(*)::int FROM proceso_evento),
    'ultimo_evento_at', (SELECT max(created_at) FROM proceso_evento),
    'primer_evento_at', (SELECT min(created_at) FROM proceso_evento),

    -- Estado real de chunks/embeddings (en vivo desde chunks_tdr)
    'chunks_pdf', (SELECT count(*)::int FROM chunks_tdr WHERE fuente = 'pdf'),
    'chunks_api', (SELECT count(*)::int FROM chunks_tdr WHERE fuente = 'api'),
    'chunks_total', (SELECT count(*)::int FROM chunks_tdr),
    'embebidos_v2', (SELECT count(*)::int FROM chunks_tdr WHERE embedding_v2 IS NOT NULL),
    'cobertura_emb_pct', CASE
      WHEN (SELECT count(*) FROM chunks_tdr) > 0
      THEN round(
        100.0 * (SELECT count(*) FROM chunks_tdr WHERE embedding_v2 IS NOT NULL)
              / (SELECT count(*) FROM chunks_tdr), 2)
      ELSE 0 END,

    -- Costo de embedding (etapas que embeben: embedded + migrado_300_60)
    'costo_embed_usd', coalesce((
      SELECT sum(costo_usd) FROM proceso_evento
      WHERE etapa IN ('embedded', 'migrado_300_60')), 0),
    'tokens_embed_est', coalesce((
      SELECT sum(tokens_est) FROM proceso_evento
      WHERE etapa IN ('embedded', 'migrado_300_60')), 0),

    -- Distribución por etapa (eventos, contratos, costo, chunks generados)
    'por_etapa', coalesce((
      SELECT jsonb_agg(x ORDER BY x->>'eventos' DESC)
      FROM (
        SELECT jsonb_build_object(
          'etapa', etapa,
          'eventos', count(*)::int,
          'contratos', count(DISTINCT contrato_id)::int,
          'costo_usd', coalesce(sum(costo_usd), 0),
          'chunks_pdf_sum', coalesce(sum(n_chunks_pdf), 0),
          'chars_sum', coalesce(sum(chars_tdr), 0)
        ) AS x
        FROM proceso_evento
        GROUP BY etapa
      ) t
    ), '[]'::jsonb),

    -- Distribución por chunk_version (sobre contratos trazados)
    'por_chunk_version', coalesce((
      SELECT jsonb_agg(x ORDER BY x->>'contratos' DESC)
      FROM (
        SELECT jsonb_build_object(
          'chunk_version', coalesce(c.chunk_version, '500_0'),
          'contratos', count(*)::int
        ) AS x
        FROM contratos c
        WHERE EXISTS (SELECT 1 FROM proceso_evento pe WHERE pe.contrato_id = c.id)
        GROUP BY coalesce(c.chunk_version, '500_0')
      ) t
    ), '[]'::jsonb),

    -- Distribución por tipo de extracción
    'por_tipo_extraccion', coalesce((
      SELECT jsonb_agg(x ORDER BY x->>'contratos' DESC)
      FROM (
        SELECT jsonb_build_object(
          'tipo_extraccion', coalesce(nullif(tdr_tipo_extraccion, ''), 'sin_dato'),
          'contratos', count(*)::int
        ) AS x
        FROM contratos c
        WHERE EXISTS (SELECT 1 FROM proceso_evento pe WHERE pe.contrato_id = c.id)
        GROUP BY coalesce(nullif(tdr_tipo_extraccion, ''), 'sin_dato')
      ) t
    ), '[]'::jsonb),

    -- Serie temporal (por día, en Lima) de eventos y costo
    'por_dia', coalesce((
      SELECT jsonb_agg(x ORDER BY x->>'dia')
      FROM (
        SELECT jsonb_build_object(
          'dia', to_char((created_at AT TIME ZONE 'America/Lima')::date, 'YYYY-MM-DD'),
          'eventos', count(*)::int,
          'costo_usd', coalesce(sum(costo_usd), 0)
        ) AS x
        FROM proceso_evento
        GROUP BY to_char((created_at AT TIME ZONE 'America/Lima')::date, 'YYYY-MM-DD')
      ) t
    ), '[]'::jsonb)
  ) INTO v;

  RETURN v;
END;
$$;

REVOKE ALL ON FUNCTION public.fn_proceso_evento_resumen() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.fn_proceso_evento_resumen() TO authenticated;

COMMENT ON FUNCTION public.fn_proceso_evento_resumen() IS
  'KPIs globales del seguimiento de proceso: eventos, chunks/embeddings, costo, distribución por etapa/chunk_version/tipo y serie diaria. Solo es_admin().';


-- ─────────────────────────────────────────────────────────────────────
-- 2) Timeline de eventos de un contrato (drill-down)
-- ─────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.fn_proceso_eventos(p_contrato_id bigint)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v jsonb;
BEGIN
  IF NOT public.es_admin() THEN
    RAISE EXCEPTION 'admin_required' USING ERRCODE = '42501';
  END IF;

  SELECT coalesce((
    SELECT jsonb_agg(x ORDER BY x->>'created_at' DESC, x->>'id' DESC)
    FROM (
      SELECT jsonb_build_object(
        'id', id,
        'created_at', created_at,
        'etapa', etapa,
        'n_chunks_pdf', n_chunks_pdf,
        'n_chunks_api', n_chunks_api,
        'chars_tdr', chars_tdr,
        'tokens_est', tokens_est,
        'costo_usd', costo_usd,
        'tipo_extraccion', tipo_extraccion,
        'chunk_version', chunk_version,
        'run_id', run_id,
        'detalle', detalle
      ) AS x
      FROM proceso_evento
      WHERE contrato_id = p_contrato_id
    ) t
  ), '[]'::jsonb) INTO v;

  RETURN v;
END;
$$;

REVOKE ALL ON FUNCTION public.fn_proceso_eventos(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.fn_proceso_eventos(bigint) TO authenticated;

COMMENT ON FUNCTION public.fn_proceso_eventos(bigint) IS
  'Timeline completo de eventos (proceso_evento) de un contrato, ordenado por fecha descendente. Solo es_admin().';
