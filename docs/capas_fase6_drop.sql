-- Capas fase 6: DROP de categoria_it y relevancia_ia en contratos.
-- IDEMPOTENTE: migraciones_datos.nombre='capas_fase6_drop'.
--
-- PRE-REQUISITOS:
--   1. Todos los lectores migrados a v_contratos / clasificacion_contrato.
--   2. Snapshot capas_fase6_snapshot aplicado.
--   3. v_contratos ya apunta a clasificacion_contrato (LEFT JOIN).
--
-- ORDEN:
--   1. Quitar trigger de eco (usa contratos.categoria_it / relevancia_ia).
--   2. Quitar funciones/vistas dependientes de contratos.categoria_it en orden inverso.
--   3. DROP COLUMN categoria_it, relevancia_ia.
--   4. Recrear v_contratos y dependientes con security_invoker=true.
--   5. Verificar columnas dentro de la transaccion.

DO $do$
DECLARE
  n_cols int;
  n_v_contratos int;
  n_post int;
  n_kpis int;
BEGIN
  CREATE TABLE IF NOT EXISTS migraciones_datos (
    nombre          text PRIMARY KEY,
    aplicada_utc    timestamptz NOT NULL DEFAULT now(),
    filas_afectadas int,
    detalle         jsonb
  );

  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'capas_fase6_drop'
  ) THEN
    RAISE NOTICE 'capas_fase6_drop ya aplicada. No-op.';
    RETURN;
  END IF;

  RAISE NOTICE 'Iniciando capas_fase6_drop...';

  -- 1. Quitar el eco (fase 4). Su funcion hace UPDATE contratos(...) con categoria_it/relevancia_ia.
  DROP TRIGGER IF EXISTS trg_clasificacion_echo ON public.clasificacion_contrato;
  DROP FUNCTION IF EXISTS public.fn_clasificacion_echo();

  -- 2. Quitar vistas/funciones dependientes en orden inverso.
  DROP VIEW IF EXISTS public.v_kpis_conversion_rubro;
  DROP VIEW IF EXISTS public.v_kpis_conversion;
  DROP VIEW IF EXISTS public.v_kpis_negocio;
  DROP VIEW IF EXISTS public.v_kpis_dashboard;
  DROP VIEW IF EXISTS public.v_contratos_estado;
  DROP VIEW IF EXISTS public.dashboard_resumen;
  DROP VIEW IF EXISTS public.vigentes_urgentes;
  DROP FUNCTION IF EXISTS public.buscar_contratos(TEXT, TEXT, TEXT, TEXT, INT, INT);
  DROP VIEW IF EXISTS public.v_contratos;

  -- 3. DROP de las columnas.
  ALTER TABLE public.contratos
    DROP COLUMN IF EXISTS categoria_it,
    DROP COLUMN IF EXISTS relevancia_ia;

  -- 4. Recrear v_contratos (41 columnas: contratos - 2 + 2 de clasificacion_contrato).
  CREATE OR REPLACE VIEW public.v_contratos AS
  SELECT
    c.id,
    c.nro_contratacion,
    c.descripcion_contrato,
    c.objeto,
    c.descripcion,
    c.entidad,
    c.estado,
    c.fecha_publica,
    c.fecha_ini_cotizacion,
    c.fecha_fin_cotizacion,
    c.tipo_cotizacion,
    c.cotizar,
    cl.categoria_it,
    cl.relevancia_ia,
    c.texto_busqueda,
    c.created_at,
    c.nom_area_usuaria,
    c.items_json,
    c.detalle_cargado,
    c.req_url,
    c.pdf_descargado,
    c.pdf_procesado,
    c.pdf_es_imagen,
    c.tdr_texto,
    c.pdf_hash,
    c.estado_verificado_at,
    c.pdf_archivo_id,
    c.pdf_nombre,
    c.tdr_tipo_extraccion,
    c.paginas_ocr_pendientes,
    c.paginas_ocr_hechas,
    c.tdr_n_paginas,
    c.tdr_n_paginas_nativas,
    c.tdr_n_paginas_ocr,
    c.analizado,
    c.cotizado,
    c.fecha_analisis,
    c.fecha_cotizacion,
    c.pdf_storage_path,
    c.pdf_storage_at,
    c.pdf_storage_bytes
  FROM public.contratos c
  LEFT JOIN public.clasificacion_contrato cl ON cl.contrato_id = c.id;

  ALTER VIEW public.v_contratos SET (security_invoker = true);
  GRANT SELECT ON public.v_contratos TO anon, authenticated;

  COMMENT ON VIEW public.v_contratos IS
    'Fase 6: vista de compatibilidad. categoria_it/relevancia_ia vienen de clasificacion_contrato.';

  -- 4b. v_contratos_estado
  CREATE OR REPLACE VIEW public.v_contratos_estado AS
  SELECT
    c.id,
    c.nro_contratacion,
    c.descripcion_contrato,
    c.objeto,
    c.descripcion,
    c.entidad,
    c.estado,
    c.fecha_publica,
    c.fecha_ini_cotizacion,
    c.fecha_fin_cotizacion,
    c.tipo_cotizacion,
    c.cotizar,
    c.categoria_it,
    c.relevancia_ia,
    c.nom_area_usuaria,
    public.seace_hoy_lima() AS hoy_lima,
    public.seace_fecha_lima(c.fecha_fin_cotizacion) AS fecha_fin_lima,
    public.seace_fecha_lima(c.fecha_publica) AS fecha_publica_lima,
    public.seace_rubro_linea(c.categoria_it) AS rubro,
    (
      c.estado = 'Vigente'
      AND (c.fecha_ini_cotizacion IS NULL OR c.fecha_ini_cotizacion <= now())
      AND (c.fecha_fin_cotizacion IS NULL OR c.fecha_fin_cotizacion >= now())
    ) AS es_postulable,
    (
      c.estado = 'Vigente'
      AND c.fecha_fin_cotizacion IS NOT NULL
      AND c.fecha_fin_cotizacion < now()
    ) AS es_vigente_ventana_vencida,
    (c.estado = 'En Evaluación') AS es_en_evaluacion,
    (c.estado = 'Culminado') AS es_culminado,
    (
      c.estado = 'Vigente'
      AND c.fecha_fin_cotizacion >= now()
      AND c.fecha_fin_cotizacion <= (public.seace_hoy_lima() + time '23:59:59.999') AT TIME ZONE 'America/Lima'
    ) AS cierra_hoy,
    (
      c.estado = 'Vigente'
      AND public.seace_fecha_lima(c.fecha_fin_cotizacion) = public.seace_hoy_lima() + 1
    ) AS cierra_manana,
    (
      c.estado = 'Vigente'
      AND public.seace_fecha_lima(c.fecha_fin_cotizacion)
        BETWEEN public.seace_hoy_lima() AND public.seace_hoy_lima() + 7
    ) AS cierra_7d,
    (
      c.estado = 'Vigente'
      AND public.seace_fecha_lima(c.fecha_fin_cotizacion)
        BETWEEN public.seace_hoy_lima() + 2 AND public.seace_hoy_lima() + 7
    ) AS cierra_semana,
    (public.seace_fecha_lima(c.fecha_publica) = public.seace_hoy_lima()) AS es_nuevo_hoy,
    (
      c.estado = 'Vigente'
      AND c.fecha_ini_cotizacion > now()
    ) AS es_por_abrir
  FROM public.v_contratos c
  WHERE c.categoria_it IS NOT NULL OR c.relevancia_ia IS NOT NULL;

  ALTER VIEW public.v_contratos_estado SET (security_invoker = true);
  GRANT SELECT ON public.v_contratos_estado TO anon, authenticated;

  -- 4c. v_kpis_conversion y v_kpis_conversion_rubro
  CREATE OR REPLACE VIEW public.v_kpis_conversion AS
  WITH universo AS (
    SELECT
      c.id,
      c.analizado,
      c.cotizado,
      v.es_postulable
    FROM public.v_contratos c
    INNER JOIN public.v_contratos_estado v ON v.id = c.id
    WHERE c.fecha_publica >= now() - interval '30 days'
  )
  SELECT
    count(*)::int AS rankeados_30d,
    count(*) FILTER (WHERE es_postulable)::int AS postulables_30d,
    count(*) FILTER (WHERE analizado)::int AS analizados_30d,
    count(*) FILTER (WHERE cotizado)::int AS cotizados_30d,
    count(*) FILTER (WHERE analizado AND es_postulable)::int AS analizados_post_30d,
    count(*) FILTER (WHERE cotizado AND es_postulable)::int AS cotizados_post_30d,
    round(
      count(*) FILTER (WHERE analizado)::numeric
      / NULLIF(count(*), 0),
      4
    ) AS cob_analisis,
    round(
      count(*) FILTER (WHERE cotizado)::numeric
      / NULLIF(count(*) FILTER (WHERE analizado), 0),
      4
    ) AS cob_cotizacion,
    round(
      count(*) FILTER (WHERE cotizado)::numeric
      / NULLIF(count(*), 0),
      4
    ) AS cob_global,
    round(
      count(*) FILTER (WHERE analizado AND es_postulable)::numeric
      / NULLIF(count(*) FILTER (WHERE es_postulable), 0),
      4
    ) AS eje_analisis,
    round(
      count(*) FILTER (WHERE cotizado AND es_postulable)::numeric
      / NULLIF(count(*) FILTER (WHERE analizado AND es_postulable), 0),
      4
    ) AS eje_cotizacion,
    round(
      count(*) FILTER (WHERE cotizado AND es_postulable)::numeric
      / NULLIF(count(*) FILTER (WHERE es_postulable), 0),
      4
    ) AS eje_global
  FROM universo;

  CREATE OR REPLACE VIEW public.v_kpis_conversion_rubro AS
  WITH universo AS (
    SELECT
      c.id,
      c.analizado,
      c.cotizado,
      v.es_postulable,
      coalesce(v.rubro, 'sin_clasificar') AS rubro
    FROM public.v_contratos c
    INNER JOIN public.v_contratos_estado v ON v.id = c.id
    WHERE c.fecha_publica >= now() - interval '30 days'
  )
  SELECT
    rubro,
    count(*)::int AS rankeados_30d,
    count(*) FILTER (WHERE es_postulable)::int AS postulables_30d,
    count(*) FILTER (WHERE analizado)::int AS analizados_30d,
    count(*) FILTER (WHERE cotizado)::int AS cotizados_30d,
    count(*) FILTER (WHERE analizado AND es_postulable)::int AS analizados_post_30d,
    count(*) FILTER (WHERE cotizado AND es_postulable)::int AS cotizados_post_30d,
    round(
      count(*) FILTER (WHERE analizado)::numeric
      / NULLIF(count(*), 0),
      4
    ) AS cob_analisis,
    round(
      count(*) FILTER (WHERE cotizado)::numeric
      / NULLIF(count(*) FILTER (WHERE analizado), 0),
      4
    ) AS cob_cotizacion,
    round(
      count(*) FILTER (WHERE cotizado)::numeric
      / NULLIF(count(*), 0),
      4
    ) AS cob_global,
    round(
      count(*) FILTER (WHERE analizado AND es_postulable)::numeric
      / NULLIF(count(*) FILTER (WHERE es_postulable), 0),
      4
    ) AS eje_analisis,
    round(
      count(*) FILTER (WHERE cotizado AND es_postulable)::numeric
      / NULLIF(count(*) FILTER (WHERE analizado AND es_postulable), 0),
      4
    ) AS eje_cotizacion,
    round(
      count(*) FILTER (WHERE cotizado AND es_postulable)::numeric
      / NULLIF(count(*) FILTER (WHERE es_postulable), 0),
      4
    ) AS eje_global
  FROM universo
  GROUP BY rubro
  ORDER BY
    CASE rubro
      WHEN 'nucleo' THEN 0
      WHEN 'adyacente' THEN 1
      WHEN 'oportunista' THEN 2
      WHEN 'marginal' THEN 3
      ELSE 4
    END;

  GRANT SELECT ON public.v_kpis_conversion TO anon, authenticated;
  GRANT SELECT ON public.v_kpis_conversion_rubro TO anon, authenticated;

  -- 4d. v_kpis_dashboard
  CREATE OR REPLACE VIEW public.v_kpis_dashboard AS
  WITH hoy AS (
    SELECT public.seace_hoy_lima() AS d
  ),
  it AS (
    SELECT
      c.*,
      public.seace_fecha_lima(c.fecha_fin_cotizacion) AS fin,
      public.seace_fecha_lima(c.fecha_publica) AS pub
    FROM public.v_contratos c, hoy
    WHERE c.categoria_it IS NOT NULL OR c.relevancia_ia IS NOT NULL
  ),
  post AS (
    SELECT *
    FROM it
    WHERE estado = 'Vigente'
      AND (fecha_ini_cotizacion IS NULL OR fecha_ini_cotizacion <= now())
      AND (fecha_fin_cotizacion IS NULL OR fecha_fin_cotizacion >= now())
  )
  SELECT
    (SELECT count(*)::int FROM post) AS total_postulables,
    (SELECT count(*)::int FROM post
      WHERE fecha_fin_cotizacion >= now()
        AND fecha_fin_cotizacion <= ((SELECT d FROM hoy) + time '23:59:59.999') AT TIME ZONE 'America/Lima'
    ) AS cierran_hoy,
    (SELECT count(*)::int FROM post WHERE fin = (SELECT d FROM hoy) + 1) AS cierran_manana,
    (SELECT count(*)::int FROM post WHERE fin BETWEEN (SELECT d FROM hoy) + 2 AND (SELECT d FROM hoy) + 7) AS cierran_semana,
    (SELECT count(*)::int FROM post WHERE pub = (SELECT d FROM hoy)) AS nuevos_hoy_postulables,
    (SELECT count(*)::int FROM it
      WHERE estado = 'Vigente'
        AND fecha_fin_cotizacion IS NOT NULL
        AND fecha_fin_cotizacion < now()
    ) AS vigentes_ventana_vencida,
    (SELECT count(*)::int FROM it WHERE estado = 'En Evaluación') AS en_evaluacion,
    (SELECT count(*)::int FROM it WHERE estado = 'Culminado') AS culminados_it,
    (SELECT count(*)::int FROM it WHERE pub BETWEEN (SELECT d FROM hoy) - 6 AND (SELECT d FROM hoy)) AS altas_it_7d,
    (SELECT count(*)::int FROM it WHERE pub BETWEEN (SELECT d FROM hoy) - 13 AND (SELECT d FROM hoy) - 7) AS altas_it_7d_prev,
    (
      SELECT coalesce(jsonb_agg(x ORDER BY x.total DESC), '[]'::jsonb)
      FROM (
        SELECT coalesce(categoria_it, '(sin línea)') AS linea, count(*)::int AS total
        FROM post
        GROUP BY 1
      ) x
    ) AS por_linea,
    (
      SELECT coalesce(jsonb_agg(x ORDER BY
        CASE x.rubro
          WHEN 'nucleo' THEN 0 WHEN 'adyacente' THEN 1
          WHEN 'oportunista' THEN 2 WHEN 'marginal' THEN 3 ELSE 4
        END), '[]'::jsonb)
      FROM (
        SELECT
          coalesce(
            public.fn_rubro_energetic(categoria_it, relevancia_ia, descripcion, descripcion_contrato),
            'sin_clasificar'
          ) AS rubro,
          count(*)::int AS total
        FROM post
        GROUP BY 1
      ) x
    ) AS por_rubro;

  ALTER VIEW public.v_kpis_dashboard SET (security_invoker = true);
  GRANT SELECT ON public.v_kpis_dashboard TO anon, authenticated;

  -- 4d. v_kpis_negocio
  CREATE OR REPLACE VIEW public.v_kpis_negocio AS
  WITH post AS (
    SELECT *
    FROM public.v_contratos
    WHERE (categoria_it IS NOT NULL OR relevancia_ia IS NOT NULL)
      AND estado = 'Vigente'
      AND (fecha_ini_cotizacion IS NULL OR fecha_ini_cotizacion <= now())
      AND (fecha_fin_cotizacion IS NULL OR fecha_fin_cotizacion >= now())
  ),
  scored AS (
    SELECT
      p.*,
      public.fn_rubro_energetic(categoria_it, relevancia_ia, descripcion, descripcion_contrato) AS rubro,
      public.seace_overlay(coalesce(descripcion, '') || ' ' || coalesce(descripcion_contrato, '')) AS overlay
    FROM post p
  )
  SELECT
    count(*) FILTER (WHERE rubro = 'nucleo')::int AS nucleo_postulables,
    count(*) FILTER (WHERE rubro = 'adyacente')::int AS adyacente_postulables,
    count(*) FILTER (WHERE rubro = 'oportunista')::int AS oportunista_postulables,
    count(*) FILTER (WHERE rubro = 'marginal')::int AS marginal_postulables,
    count(*) FILTER (WHERE rubro IS NULL)::int AS sin_rubro_postulables,
    count(*) FILTER (WHERE categoria_it = 'IA/analytics')::int AS nucleo_ia,
    count(*) FILTER (WHERE categoria_it = 'Cloud/hosting')::int AS nucleo_cloud,
    count(*) FILTER (WHERE categoria_it = 'Desarrollo software')::int AS nucleo_dev,
    count(*) FILTER (WHERE overlay = 'telemetria')::int AS nucleo_tel,
    (
      SELECT coalesce(jsonb_agg(x ORDER BY x.total DESC), '[]'::jsonb)
      FROM (
        SELECT coalesce(categoria_it, '(sin línea)') AS linea, count(*)::int AS total
        FROM scored
        GROUP BY 1
      ) x
    ) AS por_linea,
    (
      SELECT coalesce(jsonb_agg(x ORDER BY
        CASE x.rubro
          WHEN 'nucleo' THEN 0 WHEN 'adyacente' THEN 1
          WHEN 'oportunista' THEN 2 WHEN 'marginal' THEN 3 ELSE 4
        END), '[]'::jsonb)
      FROM (
        SELECT coalesce(rubro, 'sin_clasificar') AS rubro, count(*)::int AS total
        FROM scored
        GROUP BY 1
      ) x
    ) AS por_rubro
  FROM scored;

  ALTER VIEW public.v_kpis_negocio SET (security_invoker = true);
  GRANT SELECT ON public.v_kpis_negocio TO anon, authenticated;

  -- 4e. dashboard_resumen y vigentes_urgentes
  CREATE OR REPLACE VIEW public.dashboard_resumen AS
  SELECT
    objeto,
    estado,
    categoria_it,
    DATE_TRUNC('month', fecha_publica)::DATE AS mes,
    COUNT(*)::INT                            AS total
  FROM public.v_contratos
  GROUP BY
    objeto,
    estado,
    categoria_it,
    DATE_TRUNC('month', fecha_publica)::DATE;

  GRANT SELECT ON public.dashboard_resumen TO anon, authenticated;

  CREATE OR REPLACE VIEW public.vigentes_urgentes AS
  SELECT *
  FROM public.v_contratos
  WHERE estado = 'Vigente'
  ORDER BY fecha_fin_cotizacion ASC NULLS LAST;

  GRANT SELECT ON public.vigentes_urgentes TO anon, authenticated;

  -- 4f. buscar_contratos
  CREATE OR REPLACE FUNCTION public.buscar_contratos(
    termino         TEXT  DEFAULT '',
    filtro_objeto   TEXT  DEFAULT NULL,
    filtro_estado   TEXT  DEFAULT NULL,
    filtro_entidad  TEXT  DEFAULT NULL,
    limite          INT   DEFAULT 50,
    offset_val      INT   DEFAULT 0
  )
  RETURNS TABLE (
    id                    BIGINT,
    nro_contratacion      TEXT,
    descripcion_contrato  TEXT,
    objeto                TEXT,
    descripcion           TEXT,
    entidad               TEXT,
    estado                TEXT,
    fecha_publica         TIMESTAMPTZ,
    fecha_ini_cotizacion  TIMESTAMPTZ,
    fecha_fin_cotizacion  TIMESTAMPTZ,
    tipo_cotizacion       TEXT,
    cotizar               BOOLEAN,
    categoria_it          TEXT,
    relevancia_ia         TEXT,
    rank                  REAL
  )
  LANGUAGE plpgsql SECURITY DEFINER
  SET search_path = public
  AS $$
  DECLARE
    tsq tsquery;
  BEGIN
    BEGIN
      IF termino IS NOT NULL AND trim(termino) <> '' THEN
        tsq := plainto_tsquery('spanish', termino);
      END IF;
    EXCEPTION WHEN OTHERS THEN
      tsq := NULL;
    END;

    RETURN QUERY
    SELECT
      c.id,
      c.nro_contratacion,
      c.descripcion_contrato,
      c.objeto,
      c.descripcion,
      c.entidad,
      c.estado,
      c.fecha_publica,
      c.fecha_ini_cotizacion,
      c.fecha_fin_cotizacion,
      c.tipo_cotizacion,
      c.cotizar,
      c.categoria_it,
      c.relevancia_ia,
      CASE
        WHEN tsq IS NOT NULL THEN ts_rank(c.texto_busqueda, tsq)
        ELSE 1.0::REAL
      END AS rank
    FROM public.v_contratos c
    WHERE
      (tsq IS NULL OR c.texto_busqueda @@ tsq)
      AND (filtro_objeto  IS NULL OR c.objeto  = filtro_objeto)
      AND (filtro_estado  IS NULL OR c.estado  = filtro_estado)
      AND (filtro_entidad IS NULL OR c.entidad ILIKE '%' || filtro_entidad || '%')
    ORDER BY
      CASE WHEN tsq IS NOT NULL THEN ts_rank(c.texto_busqueda, tsq)
           ELSE 1.0::REAL END DESC,
      c.fecha_publica DESC NULLS LAST
    LIMIT  LEAST(limite, 200)
    OFFSET offset_val;
  END;
  $$;

  GRANT EXECUTE ON FUNCTION public.buscar_contratos(TEXT, TEXT, TEXT, TEXT, INT, INT)
    TO anon, authenticated;

  -- 5. Verificaciones.
  -- 5a. contratos ya no tiene categoria_it ni relevancia_ia.
  SELECT count(*) INTO n_cols
  FROM information_schema.columns
  WHERE table_schema = 'public'
    AND table_name = 'contratos'
    AND column_name IN ('categoria_it', 'relevancia_ia');
  IF n_cols <> 0 THEN
    RAISE EXCEPTION 'capas_fase6_drop: contratos aun tiene categoria_it/relevancia_ia. columnas=%', n_cols;
  END IF;

  -- 5b. v_contratos expone categoria_it y relevancia_ia.
  SELECT count(*) INTO n_cols
  FROM information_schema.columns
  WHERE table_schema = 'public'
    AND table_name = 'v_contratos'
    AND column_name IN ('categoria_it', 'relevancia_ia');
  IF n_cols <> 2 THEN
    RAISE EXCEPTION 'capas_fase6_drop: v_contratos no expone categoria_it/relevancia_ia. columnas=%', n_cols;
  END IF;

  -- 5c. v_contratos tiene 41 columnas (contratos - 2 + 2 de clasificacion_contrato).
  SELECT count(*) INTO n_v_contratos
  FROM information_schema.columns
  WHERE table_schema = 'public' AND table_name = 'v_contratos';
  IF n_v_contratos <> 41 THEN
    RAISE EXCEPTION 'capas_fase6_drop: v_contratos deberia tener 41 columnas; tiene %', n_v_contratos;
  END IF;

  -- 5d. v_contratos_estado y v_kpis_dashboard son legibles y devuelven filas.
  SELECT count(*) INTO n_post FROM public.v_contratos_estado;
  SELECT count(*) INTO n_kpis FROM public.v_kpis_dashboard;
  IF n_post = 0 OR n_kpis = 0 THEN
    RAISE EXCEPTION 'capas_fase6_drop: v_contratos_estado=% o v_kpis_dashboard=% sin filas', n_post, n_kpis;
  END IF;

  INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
  VALUES (
    'capas_fase6_drop',
    0,
    jsonb_build_object(
      'accion', 'DROP categoria_it, relevancia_ia de contratos; recrear vistas',
      'v_contratos_columnas', n_v_contratos,
      'v_contratos_estado_filas', n_post,
      'v_kpis_dashboard_filas', n_kpis
    )
  );

  RAISE NOTICE 'capas_fase6_drop aplicada OK. v_contratos columnas=% post=% kpis=%',
    n_v_contratos, n_post, n_kpis;

  NOTIFY pgrst, 'reload schema';
END;
$do$;
