-- Expand: KPIs de conversión del funnel #10/#11 (30 días, universo IT).
-- NO modifica v_kpis_dashboard, v_kpis_negocio, v_contratos_estado,
-- RLS, grants existentes ni la tabla contratos.
--
-- Universo base = IT publicado en 30d:
--   INNER JOIN v_contratos_estado (predicado IT en capa_semantica.sql L214)
--   AND fecha_publica >= now() - interval '30 days'
-- Postulable = v.es_postulable (fuente única, capa_semantica.sql L180-186).
--   NO se reimplementa el predicado.
-- Rubro = v.rubro = seace_rubro_linea (capa_semantica.sql L179).
--
-- Dos bloques de tasas (0..1, numeric 4 decimales; NULL si denominador 0):
--   COBERTURA  denominador = rankeados_30d   (todo el radar IT 30d)
--   EJECUCIÓN  denominador = postulables_30d (accionable = Ruta)
--
-- Invariantes: postulables_30d <= rankeados_30d
--              analizados_30d  <= rankeados_30d
--              cotizados_30d   <= analizados_30d
--              eje_* >= cob_*  (mismo numerador de post, denominador menor)
--
-- Las vistas v_kpis_* viven en capa_semantica.sql (raíz del repo).
-- Esta migración va en docs/ para no re-ejecutar su DROP ... CASCADE.

CREATE OR REPLACE VIEW v_kpis_conversion AS
WITH universo AS (
  SELECT
    c.id,
    c.analizado,
    c.cotizado,
    v.es_postulable
  FROM v_contratos c
  INNER JOIN v_contratos_estado v ON v.id = c.id
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

CREATE OR REPLACE VIEW v_kpis_conversion_rubro AS
WITH universo AS (
  SELECT
    c.id,
    c.analizado,
    c.cotizado,
    v.es_postulable,
    coalesce(v.rubro, 'sin_clasificar') AS rubro
  FROM v_contratos c
  INNER JOIN v_contratos_estado v ON v.id = c.id
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

COMMENT ON VIEW v_kpis_conversion IS
  'Funnel 30d IT: cobertura (radar publicado) vs ejecución (postulables = v_contratos_estado.es_postulable). Tasas 0..1; NULL si denominador 0.';
COMMENT ON VIEW v_kpis_conversion_rubro IS
  'Mismo universo que v_kpis_conversion, desglose por rubro (v.rubro = seace_rubro_linea). Núcleo primero.';

-- Tras aplicar en el SQL Editor de Supabase, recargar cache de PostgREST:
--   NOTIFY pgrst, 'reload schema';
--
-- GRANT de las vistas NUEVAS (no toca grants existentes). Correr a mano si el front las lee con anon:
--   GRANT SELECT ON v_kpis_conversion TO anon, authenticated;
--   GRANT SELECT ON v_kpis_conversion_rubro TO anon, authenticated;
--
-- Rollback:
--   DROP VIEW IF EXISTS v_kpis_conversion;
--   DROP VIEW IF EXISTS v_kpis_conversion_rubro;
