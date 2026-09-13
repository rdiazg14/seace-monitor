-- Vista de análisis sobre pipeline_runs (historial de corridas).
-- Thin view: expone la tabla tal cual con security_invoker=true, de modo que
-- hereda el RLS de pipeline_runs (SELECT solo es_admin) sin reimplementar la
-- policy. No se expone a anon; el front admin lee como authenticated.
--
-- Ejecutar: uv run python scripts/run_sql.py docs/vista_pipeline_runs.sql
--
-- Uso (payload es jsonb con los stats k=v de cada paso):
--   SELECT paso, ts, payload->>'total_registros' AS total
--   FROM v_pipeline_runs
--   WHERE paso = 'ingesta'
--   ORDER BY ts DESC;

CREATE OR REPLACE VIEW public.v_pipeline_runs AS
SELECT
  id,
  paso,
  run_id,
  ts,
  payload
FROM public.pipeline_runs;

ALTER VIEW public.v_pipeline_runs SET (security_invoker = true);

COMMENT ON VIEW public.v_pipeline_runs IS
  'Historial de corridas del pipeline (security_invoker=true). Hereda RLS de pipeline_runs (SELECT es_admin). payload = stats jsonb.';

GRANT SELECT ON public.v_pipeline_runs TO authenticated;

NOTIFY pgrst, 'reload schema';
