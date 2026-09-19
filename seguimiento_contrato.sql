-- =====================================================================
-- fn_seguimiento_contrato  ·  consulta admin del seguimiento por contrato
-- =====================================================================
-- Une el último evento de proceso_evento + los counts en vivo de chunks_tdr
-- para mostrar, por contrato: cuántos chunks pdf/api/total, cuántos embebidos
-- v2, qué etapa está, cuándo fue el último evento y cuánto costó el embedding
-- (acumulado). Devuelve JSONB para consumir vía RPC (patrón fn_sin_intento).
--
-- Guard: solo es_admin() (42501 si no).
-- =====================================================================

CREATE OR REPLACE FUNCTION public.fn_seguimiento_contrato(
  p_limite integer DEFAULT 50,
  p_offset integer DEFAULT 0,
  p_estado text DEFAULT NULL
)
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
    'total', (
      SELECT count(*)::int
      FROM contratos c
      WHERE (p_estado IS NULL OR c.estado = p_estado)
        AND EXISTS (SELECT 1 FROM proceso_evento pe WHERE pe.contrato_id = c.id)
    ),
    'filas', coalesce((
      SELECT jsonb_agg(x ORDER BY x->>'ultimo_evento_at' DESC)
      FROM (
        SELECT jsonb_build_object(
          'contrato_id', c.id,
          'nro_contratacion', c.nro_contratacion,
          'estado', c.estado,
          'tdr_tipo_extraccion', c.tdr_tipo_extraccion,
          'chunk_version', c.chunk_version,
          'tdr_chars', length(coalesce(c.tdr_texto, '')),
          'n_chunks_pdf', (
            SELECT count(*) FROM chunks_tdr ch
            WHERE ch.contrato_id = c.id AND ch.fuente = 'pdf'
          ),
          'n_chunks_api', (
            SELECT count(*) FROM chunks_tdr ch
            WHERE ch.contrato_id = c.id AND ch.fuente = 'api'
          ),
          'n_chunks_total', (
            SELECT count(*) FROM chunks_tdr ch
            WHERE ch.contrato_id = c.id
          ),
          'n_embebidos_v2', (
            SELECT count(*) FROM chunks_tdr ch
            WHERE ch.contrato_id = c.id AND ch.embedding_v2 IS NOT NULL
          ),
          'ultima_etapa', (
            SELECT pe.etapa FROM proceso_evento pe
            WHERE pe.contrato_id = c.id
            ORDER BY pe.created_at DESC, pe.id DESC LIMIT 1
          ),
          'ultimo_evento_at', (
            SELECT pe.created_at FROM proceso_evento pe
            WHERE pe.contrato_id = c.id
            ORDER BY pe.created_at DESC, pe.id DESC LIMIT 1
          ),
          'costo_embed_acum', coalesce((
            SELECT sum(pe.costo_usd) FROM proceso_evento pe
            WHERE pe.contrato_id = c.id AND pe.etapa = 'embedded'
          ), 0),
          'n_eventos', (
            SELECT count(*) FROM proceso_evento pe
            WHERE pe.contrato_id = c.id
          )
        ) AS x
        FROM contratos c
        WHERE (p_estado IS NULL OR c.estado = p_estado)
          AND EXISTS (SELECT 1 FROM proceso_evento pe WHERE pe.contrato_id = c.id)
        ORDER BY (
          SELECT max(pe.created_at) FROM proceso_evento pe
          WHERE pe.contrato_id = c.id
        ) DESC
        LIMIT p_limite OFFSET p_offset
      ) t
    ), '[]'::jsonb)
  ) INTO v;

  RETURN v;
END;
$$;

REVOKE ALL ON FUNCTION public.fn_seguimiento_contrato(integer, integer, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.fn_seguimiento_contrato(integer, integer, text) TO authenticated;

COMMENT ON FUNCTION public.fn_seguimiento_contrato(integer, integer, text) IS
  'Seguimiento por contrato: último evento + counts de chunks/embeddings v2 + costo de embedding acumulado. Solo es_admin().';
