-- ============================================================
-- Capa semántica ENERTRONIC — fuente de verdad de métricas
-- Ejecutar en: Supabase → SQL Editor → Run
-- Idempotente (CREATE OR REPLACE).
--
-- Postulable = instante (ini <= now <= fin). Día Lima solo para tramos
--   de cierre (mañana / semana 2–7) y altas 7d. Gemelo de esPostulable().
--
-- Rubro en KPIs: fn_rubro_energetic = gemelo de clasificarNivel()
--   (rutaDia.ts). Solo se evalúa sobre postulables (conjunto chico).
-- v_contratos_estado: flags baratos (estado + fechas). rubro_linea =
--   mapeo categoria_it, sin regex, para listados.
--
-- Funnel #10/#11: columnas analizado/cotizado/fecha_* viven en contratos
-- (docs/migracion_funnel_conversion.sql). Las vistas de conversión 30d
-- NO están aquí (docs/vista_kpis_conversion.sql) para no re-DROP CASCADE.
-- No hay monto referencial (SEACE no lo expone en las APIs que usamos).
-- ============================================================

DROP VIEW IF EXISTS v_kpis_negocio CASCADE;
DROP VIEW IF EXISTS v_kpis_dashboard CASCADE;
DROP VIEW IF EXISTS v_contratos_estado CASCADE;

CREATE OR REPLACE FUNCTION seace_hoy_lima()
RETURNS date
LANGUAGE sql
STABLE
AS $$
  SELECT (timezone('America/Lima', now()))::date;
$$;

CREATE OR REPLACE FUNCTION seace_fecha_lima(ts timestamptz)
RETURNS date
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT (timezone('America/Lima', ts))::date;
$$;

CREATE OR REPLACE FUNCTION seace_norm(t text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT lower(translate(
    coalesce(t, ''),
    'áéíóúüñÁÉÍÓÚÜÑàèìòùÀÈÌÒÙ',
    'aeiouunAEIOUUNaeiouAEIOU'
  ));
$$;

CREATE OR REPLACE FUNCTION seace_tiene_ia_real(txt text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT seace_norm(txt) ~ (
    'gpt|llm|copilot|machine learning|aprendizaje automatico|'
    || 'inteligencia artificial|ia generativa|deep learning|red neuronal|'
    || 'modelo de lenguaje|azure openai|openai|claude|gemini|chatbot|'
    || 'asistente virtual|ciencia de datos|big data|'
    || 'tokens de procesamiento|tokens de ia'
  );
$$;

CREATE OR REPLACE FUNCTION seace_overlay(txt text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT CASE
    WHEN t ~ 'telemetria|scada|internet de las cosas|tecnologia operacional|tecnologias operacionales'
      OR t ~ '(^|[^a-z0-9])ot([^a-z0-9]|$)'
      OR t ~ '(^|[^a-z0-9])iot([^a-z0-9]|$)'
      THEN 'telemetria'
    WHEN t ~ 'digital twin|gemelo digital|integracion|automatizacion'
      THEN 'integracion'
    ELSE NULL
  END
  FROM (SELECT seace_norm(txt) AS t) s;
$$;

CREATE OR REPLACE FUNCTION seace_max_rubro(a text, b text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT CASE
    WHEN a IS NULL THEN b
    WHEN b IS NULL THEN a
    WHEN rank_a >= rank_b THEN a
    ELSE b
  END
  FROM (
    SELECT
      CASE a
        WHEN 'nucleo' THEN 3 WHEN 'adyacente' THEN 2
        WHEN 'oportunista' THEN 1 WHEN 'marginal' THEN 0 ELSE -1
      END AS rank_a,
      CASE b
        WHEN 'nucleo' THEN 3 WHEN 'adyacente' THEN 2
        WHEN 'oportunista' THEN 1 WHEN 'marginal' THEN 0 ELSE -1
      END AS rank_b
  ) r;
$$;

CREATE OR REPLACE FUNCTION seace_rubro_linea(p_categoria_it text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT CASE p_categoria_it
    WHEN 'IA/analytics' THEN 'nucleo'
    WHEN 'Cloud/hosting' THEN 'nucleo'
    WHEN 'Desarrollo software' THEN 'nucleo'
    WHEN 'Telemetria/OT' THEN 'nucleo'
    WHEN 'Base de datos/ERP' THEN 'adyacente'
    WHEN 'Oracle' THEN 'adyacente'
    WHEN 'Soporte tecnico' THEN 'oportunista'
    WHEN 'Redes/cableado' THEN 'oportunista'
    WHEN 'Licencias' THEN 'oportunista'
    WHEN 'Ciberseguridad' THEN 'oportunista'
    WHEN 'Microsoft' THEN 'oportunista'
    WHEN 'Correo electronico' THEN 'oportunista'
    WHEN 'Firma digital' THEN 'oportunista'
    WHEN 'Hardware' THEN 'marginal'
    ELSE NULL
  END;
$$;

-- Gemelo de clasificarNivel() (rutaDia.ts). Overlay nunca degrada.
CREATE OR REPLACE FUNCTION fn_rubro_energetic(
  p_categoria_it text,
  p_relevancia_ia text,
  p_descripcion text,
  p_descripcion_contrato text
)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
  txt text := coalesce(p_descripcion, '') || ' ' || coalesce(p_descripcion_contrato, '');
  ov text := seace_overlay(txt);
  nivel text := seace_rubro_linea(p_categoria_it);
BEGIN
  IF ov = 'telemetria' THEN
    nivel := seace_max_rubro(nivel, 'nucleo');
  ELSIF ov = 'integracion' THEN
    nivel := seace_max_rubro(nivel, 'adyacente');
  END IF;

  IF upper(coalesce(p_relevancia_ia, '')) = 'ALTA'
     AND seace_tiene_ia_real(txt)
     AND coalesce(p_categoria_it, '') <> 'Firma digital' THEN
    nivel := seace_max_rubro(nivel, 'nucleo');
  END IF;

  RETURN nivel;
END;
$$;

CREATE OR REPLACE VIEW v_contratos AS
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
FROM contratos c
LEFT JOIN clasificacion_contrato cl ON cl.contrato_id = c.id;

GRANT SELECT ON v_contratos TO anon, authenticated;

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
FROM v_contratos c
WHERE c.categoria_it IS NOT NULL OR c.relevancia_ia IS NOT NULL;

CREATE OR REPLACE VIEW v_kpis_dashboard AS
WITH hoy AS (
  SELECT seace_hoy_lima() AS d
),
it AS (
  SELECT
    c.*,
    seace_fecha_lima(c.fecha_fin_cotizacion) AS fin,
    seace_fecha_lima(c.fecha_publica) AS pub
  FROM v_contratos c, hoy
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
  ) AS por_rubro;

CREATE OR REPLACE VIEW v_kpis_negocio AS
WITH post AS (
  SELECT *
  FROM v_contratos
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
FROM scored;

GRANT SELECT ON v_contratos_estado TO anon, authenticated;
GRANT SELECT ON v_kpis_dashboard TO anon, authenticated;
GRANT SELECT ON v_kpis_negocio TO anon, authenticated;

COMMENT ON VIEW v_contratos_estado IS
  'Universo IT/IA desde v_contratos (capa 3). es_postulable = instante (ini<=now<=fin). es_por_abrir = ini futura. cierran_hoy = now..fin dia Lima. rubro = mapeo linea.';
COMMENT ON VIEW v_kpis_dashboard IS
  'Agregados del tablero. total_postulables por instante. cierran_hoy = now..medianoche Lima; manana/semana por dias Lima (2-7).';
COMMENT ON VIEW v_kpis_negocio IS
  'KPIs ENERTRONIC sobre postulables (instante). Rubro = fn_rubro_energetic.';

NOTIFY pgrst, 'reload schema';
