-- Expand: log persistente de cotizar_tipo (histórico consultable por SQL).
-- No toca tablas existentes, RLS ni grants de contratos.
-- El contador diario en KV (cotizar_tipo:{tipo}:{YYYY-MM-DD}) sigue igual.
--
-- Escritura: Worker seace-ai-proxy con SUPABASE_SERVICE_KEY (service_role).
-- Lectura browser: solo admin — ver cotizar_tipo_log_select_admin.sql.
--
-- Tras aplicar en el SQL Editor de Supabase:
--   NOTIFY pgrst, 'reload schema';
-- Worker (una vez): wrangler secret put SUPABASE_SERVICE_KEY  (mismo valor que pipeline)

CREATE TABLE IF NOT EXISTS cotizar_tipo_log (
  id BIGSERIAL PRIMARY KEY,
  contrato_id BIGINT NOT NULL,
  tipo_respuesta TEXT NOT NULL,
  categoria_it TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cotizar_tipo_log_created_at ON cotizar_tipo_log (created_at);
CREATE INDEX IF NOT EXISTS idx_cotizar_tipo_log_tipo ON cotizar_tipo_log (tipo_respuesta);

COMMENT ON TABLE cotizar_tipo_log IS
  'Evento por respuesta cotizar (cache MISS). Fuente histórica; contador diario sigue en KV.';

ALTER TABLE cotizar_tipo_log ENABLE ROW LEVEL SECURITY;

-- SELECT admin (idempotente; apply-only también en cotizar_tipo_log_select_admin.sql).
GRANT SELECT ON TABLE public.cotizar_tipo_log TO authenticated;

DROP POLICY IF EXISTS cotizar_tipo_log_select_admin ON public.cotizar_tipo_log;
CREATE POLICY cotizar_tipo_log_select_admin
  ON public.cotizar_tipo_log
  FOR SELECT
  TO authenticated
  USING (public.es_admin());

COMMENT ON POLICY cotizar_tipo_log_select_admin ON public.cotizar_tipo_log IS
  'Solo perfiles.rol = admin (es_admin). Escritura sigue siendo service_role.';
