-- B22: hasta B21 las fechas estaban 5h corridas y comparar el instante
-- daba falsos negativos; por eso el criterio era dia Lima. Con las fechas
-- corregidas, un contrato que cierra hoy 15:08 no es postulable a las
-- 22:52. Medido 5 sep: 16 postulables por dia vs 10 por instante.
--
-- IDEMPOTENTE: si migraciones_datos.nombre=b22_postulable_instante, no-op.
-- CREATE OR REPLACE (sin DROP CASCADE) para no tumbar v_kpis_conversion.
-- es_por_abrir se agrega al FINAL de v_contratos_estado (PG lo permite).

DO $b22$
BEGIN
  CREATE TABLE IF NOT EXISTS migraciones_datos (
    nombre          text PRIMARY KEY,
    aplicada_utc    timestamptz NOT NULL DEFAULT now(),
    filas_afectadas int,
    detalle         jsonb
  );

  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'b22_postulable_instante'
  ) THEN
    RAISE NOTICE 'B22 ya aplicada (migraciones_datos.nombre=b22_postulable_instante). No-op.';
    RETURN;
  END IF;

  EXECUTE $v$
    CREATE OR REPLACE VIEW v_contratos_estado AS
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
      seace_hoy_lima() AS hoy_lima,
      seace_fecha_lima(c.fecha_fin_cotizacion) AS fecha_fin_lima,
      seace_fecha_lima(c.fecha_publica) AS fecha_publica_lima,
      seace_rubro_linea(c.categoria_it) AS rubro,
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
        AND c.fecha_fin_cotizacion <= (seace_hoy_lima() + time '23:59:59.999') AT TIME ZONE 'America/Lima'
      ) AS cierra_hoy,
      (
        c.estado = 'Vigente'
        AND seace_fecha_lima(c.fecha_fin_cotizacion) = seace_hoy_lima() + 1
      ) AS cierra_manana,
      (
        c.estado = 'Vigente'
        AND seace_fecha_lima(c.fecha_fin_cotizacion)
          BETWEEN seace_hoy_lima() AND seace_hoy_lima() + 7
      ) AS cierra_7d,
      (
        c.estado = 'Vigente'
        AND seace_fecha_lima(c.fecha_fin_cotizacion)
          BETWEEN seace_hoy_lima() + 2 AND seace_hoy_lima() + 7
      ) AS cierra_semana,
      (seace_fecha_lima(c.fecha_publica) = seace_hoy_lima()) AS es_nuevo_hoy,
      (
        c.estado = 'Vigente'
        AND c.fecha_ini_cotizacion > now()
      ) AS es_por_abrir
    FROM contratos c
    WHERE c.categoria_it IS NOT NULL OR c.relevancia_ia IS NOT NULL
  $v$;

  EXECUTE $v$
    CREATE OR REPLACE VIEW v_kpis_dashboard AS
    WITH hoy AS (
      SELECT seace_hoy_lima() AS d
    ),
    it AS (
      SELECT
        c.*,
        seace_fecha_lima(c.fecha_fin_cotizacion) AS fin,
        seace_fecha_lima(c.fecha_publica) AS pub
      FROM contratos c, hoy
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
              fn_rubro_energetic(categoria_it, relevancia_ia, descripcion, descripcion_contrato),
              'sin_clasificar'
            ) AS rubro,
            count(*)::int AS total
          FROM post
          GROUP BY 1
        ) x
      ) AS por_rubro
  $v$;

  EXECUTE $v$
    CREATE OR REPLACE VIEW v_kpis_negocio AS
    WITH post AS (
      SELECT *
      FROM contratos
      WHERE (categoria_it IS NOT NULL OR relevancia_ia IS NOT NULL)
        AND estado = 'Vigente'
        AND (fecha_ini_cotizacion IS NULL OR fecha_ini_cotizacion <= now())
        AND (fecha_fin_cotizacion IS NULL OR fecha_fin_cotizacion >= now())
    ),
    scored AS (
      SELECT
        p.*,
        fn_rubro_energetic(categoria_it, relevancia_ia, descripcion, descripcion_contrato) AS rubro,
        seace_overlay(coalesce(descripcion, '') || ' ' || coalesce(descripcion_contrato, '')) AS overlay
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
    FROM scored
  $v$;

  EXECUTE 'COMMENT ON VIEW v_contratos_estado IS ''Universo IT/IA. es_postulable = instante (ini<=now<=fin). es_por_abrir = ini futura. cierran_hoy = now..fin dia Lima. rubro = mapeo linea.''';
  EXECUTE 'COMMENT ON VIEW v_kpis_dashboard IS ''Agregados del tablero. total_postulables por instante. cierran_hoy = now..medianoche Lima; manana/semana por dias Lima (2-7).''';
  EXECUTE 'COMMENT ON VIEW v_kpis_negocio IS ''KPIs ENERTRONIC sobre postulables (instante). Rubro = fn_rubro_energetic.''';

  EXECUTE 'GRANT SELECT ON v_contratos_estado TO anon, authenticated';
  EXECUTE 'GRANT SELECT ON v_kpis_dashboard TO anon, authenticated';
  EXECUTE 'GRANT SELECT ON v_kpis_negocio TO anon, authenticated';

  INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
  VALUES (
    'b22_postulable_instante',
    (SELECT count(*) FROM v_contratos_estado WHERE es_postulable),
    jsonb_build_object(
      'criterio', 'instante',
      'por_abrir', (SELECT count(*) FROM v_contratos_estado WHERE es_por_abrir)
    )
  );

  EXECUTE 'NOTIFY pgrst, ''reload schema''';
  RAISE NOTICE 'B22 aplicada: postulables=% por_abrir=%',
    (SELECT count(*) FROM v_contratos_estado WHERE es_postulable),
    (SELECT count(*) FROM v_contratos_estado WHERE es_por_abrir);
END
$b22$;
