-- Fase 2A: analisis_contrato SELECT para authenticated (el front lee).
-- Sin write desde el browser. Idempotente.
-- Fase 1 daba SELECT a anon+authenticated (policy USING true).
-- El analisis es RequireAuth: alcanza authenticated.
--
-- uv run python scripts/run_sql.py docs/capas_fase2a_analisis_rls.sql

BEGIN;

REVOKE SELECT ON TABLE public.analisis_contrato FROM anon;

GRANT SELECT ON TABLE public.analisis_contrato TO authenticated;

DROP POLICY IF EXISTS analisis_select_public ON public.analisis_contrato;
DROP POLICY IF EXISTS analisis_select_authenticated ON public.analisis_contrato;
CREATE POLICY analisis_select_authenticated
  ON public.analisis_contrato
  FOR SELECT
  TO authenticated
  USING (true);

COMMENT ON POLICY analisis_select_authenticated ON public.analisis_contrato IS
  'Front autenticado lee el analisis persistido. Escritura: service_role / Worker.';

INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
VALUES (
  'capas_fase2a_analisis_rls',
  0,
  '{"tabla": "analisis_contrato", "select": "authenticated"}'::jsonb
)
ON CONFLICT (nombre) DO NOTHING;

NOTIFY pgrst, 'reload schema';

COMMIT;
