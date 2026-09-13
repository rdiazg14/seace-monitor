-- ============================================================
-- FIX performance (definitivo): dashboard_resumen como VISTA MATERIALIZADA
--
-- Contexto: con 82k+ filas en contratos, la vista regular hacía un
--   GROUP BY de tabla completa vía v_contratos (columnas TOAST) y tardaba
--   ~10s, superando el statement_timeout del rol `authenticated` (8s)
--   → "canceling statement due to statement timeout".
--
-- Solucion: materializar el agregado (solo ~837 filas) y refrescarlo
--   (1) cada 5 minutos con pg_cron (red de seguridad) y
--   (2) al final del pipeline diario (refrescar_matviews.py).
--   El refresco CONCURRENTLY no bloquea lecturas del dashboard.
--
-- Requiere la extension pg_cron (habilitada una vez con
--   CREATE EXTENSION IF NOT EXISTS pg_cron;).
--
-- Ejecutar en: Supabase -> SQL Editor -> Run (idempotente).
-- ============================================================

-- 1. Reemplazar la vista regular por la vista materializada.
--    (DROP VIEW cubre el estado actual; DROP MATERIALIZED VIEW cubre
--    el caso de re-ejecución, ya que ambos tipos coexisten por nombre.)
DROP VIEW IF EXISTS dashboard_resumen CASCADE;
DROP MATERIALIZED VIEW IF EXISTS dashboard_resumen;

CREATE MATERIALIZED VIEW dashboard_resumen AS
SELECT
  c.objeto,
  c.estado,
  cl.categoria_it,
  DATE_TRUNC('month', c.fecha_publica)::DATE AS mes,
  COUNT(*)::INT                          AS total
FROM contratos c
LEFT JOIN clasificacion_contrato cl ON cl.contrato_id = c.id
GROUP BY
  c.objeto,
  c.estado,
  cl.categoria_it,
  DATE_TRUNC('month', c.fecha_publica)::DATE;

-- 2. Indice unico (requisito de REFRESH ... CONCURRENTLY).
CREATE UNIQUE INDEX IF NOT EXISTS idx_dashboard_resumen_uq
  ON dashboard_resumen (objeto, estado, categoria_it, mes);

-- 3. Permisos (mismo alcance publico que la vista anterior).
GRANT SELECT ON dashboard_resumen TO anon, authenticated;

-- 4. Refresco programado cada 5 minutos (idempotente por jobname).
SELECT cron.unschedule(jobid) FROM cron.job WHERE jobname = 'refresh_dashboard_resumen';
SELECT cron.schedule(
  'refresh_dashboard_resumen',
  '*/5 * * * *',
  $$REFRESH MATERIALIZED VIEW CONCURRENTLY dashboard_resumen$$
);

NOTIFY pgrst, 'reload schema';
