-- Vista admin de keywords: rastro de quien escribio + RPC de simulacion.
-- IDEMPOTENTE: migraciones_datos.nombre=admin_keywords_simular.
-- El browser no llama este RPC: solo la Edge Function (service_role).

BEGIN;

ALTER TABLE public.it_keywords
  ADD COLUMN IF NOT EXISTS actualizada_utc timestamptz NOT NULL DEFAULT now();
ALTER TABLE public.it_keywords
  ADD COLUMN IF NOT EXISTS actualizada_por uuid;

COMMENT ON COLUMN public.it_keywords.actualizada_por IS
  'JWT sub del admin que creo o edito. Escritura: Edge Function service_role.';

CREATE OR REPLACE FUNCTION public.admin_simular_keyword(
  p_keyword text,
  p_categoria text,
  p_limite_palabra boolean DEFAULT false
)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  kw text;
  pri_new int;
  universo int;
  sin_clase int;
  ya int;
  cambios int;
  ejemplos jsonb;
BEGIN
  kw := seace_norm(btrim(p_keyword));
  IF kw IS NULL OR kw = '' THEN
    RAISE EXCEPTION 'keyword vacia';
  END IF;

  SELECT min(prioridad) INTO pri_new
  FROM public.it_keywords
  WHERE categoria = p_categoria;
  IF pri_new IS NULL THEN
    pri_new := 99;
  END IF;

  WITH hay AS (
    SELECT
      v.id,
      coalesce(nullif(btrim(v.descripcion), ''), v.descripcion_contrato, v.objeto) AS titulo,
      v.categoria_it,
      (' ' || seace_norm(
        coalesce(v.descripcion, '') || ' ' ||
        coalesce(v.descripcion_contrato, '') || ' ' ||
        coalesce(v.objeto, '') || ' ' ||
        coalesce(v.entidad, '')
      ) || ' ') AS t
    FROM public.v_contratos v
  ),
  hits AS (
    SELECT h.id, h.titulo, h.categoria_it
    FROM hay h
    WHERE CASE
      WHEN p_limite_palabra THEN
        h.t ~ (
          '(^|[^a-z0-9])'
          || regexp_replace(kw, '([\\.^$|?*+()\[\]{}])', '\\\1', 'g')
          || '([^a-z0-9]|$)'
        )
      ELSE strpos(h.t, kw) > 0
    END
  ),
  pri_old AS (
    SELECT DISTINCT categoria, prioridad FROM public.it_keywords
  )
  SELECT
    (SELECT count(*)::int FROM hits),
    (SELECT count(*)::int FROM hits WHERE categoria_it IS NULL),
    (SELECT count(*)::int FROM hits WHERE categoria_it IS NOT NULL),
    (
      SELECT count(*)::int
      FROM hits h
      LEFT JOIN pri_old p ON p.categoria = h.categoria_it
      WHERE h.categoria_it IS NOT NULL
        AND h.categoria_it IS DISTINCT FROM p_categoria
        AND pri_new < coalesce(p.prioridad, 99)
    ),
    coalesce((
      SELECT jsonb_agg(jsonb_build_object('id', x.id, 'titulo', x.titulo))
      FROM (
        SELECT id, titulo
        FROM (
          SELECT id, titulo, 1 AS ord FROM hits WHERE categoria_it IS NULL
          UNION ALL
          SELECT id, titulo, 2 FROM hits WHERE categoria_it IS NOT NULL
        ) u
        ORDER BY ord, id
        LIMIT 10
      ) x
    ), '[]'::jsonb)
  INTO universo, sin_clase, ya, cambios, ejemplos;

  RETURN jsonb_build_object(
    'keyword', kw,
    'categoria', p_categoria,
    'universo', universo,
    'etiquetaria', sin_clase,
    'ya_etiquetados', ya,
    'cambios_categoria', cambios,
    'ratio_predictivo', CASE
      WHEN universo = 0 THEN NULL
      ELSE round((ya::numeric / universo), 4)
    END,
    'ejemplos', ejemplos
  );
END;
$$;

REVOKE ALL ON FUNCTION public.admin_simular_keyword(text, text, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.admin_simular_keyword(text, text, boolean) TO service_role;

CREATE OR REPLACE FUNCTION public.admin_keyword_conteos()
RETURNS TABLE(keyword_id bigint, n bigint)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public
AS $$
  SELECT cl.keyword_id, count(*)::bigint
  FROM public.clasificacion_contrato cl
  WHERE cl.keyword_id IS NOT NULL
  GROUP BY cl.keyword_id
$$;

GRANT EXECUTE ON FUNCTION public.admin_keyword_conteos() TO authenticated;

COMMENT ON FUNCTION public.admin_keyword_conteos() IS
  'Conteo de etiquetas por keyword_id. Lectura: JWT admin (RLS de clasificacion es publica).';

COMMENT ON FUNCTION public.admin_simular_keyword(text, text, boolean) IS
  'Simula impacto de una keyword incluye. No escribe. Solo service_role (Edge Function).';

INSERT INTO public.migraciones_datos (nombre, filas_afectadas, detalle)
VALUES (
  'admin_keywords_simular',
  0,
  '{"rpc":"admin_simular_keyword","cols":["actualizada_utc","actualizada_por"]}'::jsonb
)
ON CONFLICT (nombre) DO NOTHING;

COMMIT;
