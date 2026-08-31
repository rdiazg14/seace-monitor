-- Expand: SELECT de cotizar_tipo_log solo para perfiles.rol = admin.
-- No toca INSERT (sigue service_role / Worker). No toca otras tablas.
-- Idempotente. Aplicar en SQL Editor o: npx supabase db query --linked -f ...

GRANT SELECT ON TABLE public.cotizar_tipo_log TO authenticated;

DROP POLICY IF EXISTS cotizar_tipo_log_select_admin ON public.cotizar_tipo_log;
CREATE POLICY cotizar_tipo_log_select_admin
  ON public.cotizar_tipo_log
  FOR SELECT
  TO authenticated
  USING (public.es_admin());

COMMENT ON POLICY cotizar_tipo_log_select_admin ON public.cotizar_tipo_log IS
  'Solo perfiles.rol = admin (es_admin). Escritura sigue siendo service_role.';

NOTIFY pgrst, 'reload schema';
