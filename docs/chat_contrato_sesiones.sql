-- Chat del asistente de contrato: persistencia en BD (antes localStorage).
--
-- Extiende las tablas de Fase 1 (chat_sesiones / chat_mensajes) para soportar
-- el chat anclado a un contrato ("¿Y si…?" en Análisis de contrato):
--   * chat_sesiones.contrato_id  → asocia una sesión a contratos.id (NULL = chat general)
--   * chat_mensajes.payload      → estado rico del mensaje del bot (escenario JSON,
--                                  clasificación, razonamiento, modelo, request_id, usage).
--
-- Idempotente (ADD COLUMN IF NOT EXISTS). Aplicar con: python scripts/run_sql.py <este archivo>

ALTER TABLE public.chat_sesiones
  ADD COLUMN IF NOT EXISTS contrato_id bigint NULL;

ALTER TABLE public.chat_mensajes
  ADD COLUMN IF NOT EXISTS payload jsonb NULL;

CREATE INDEX IF NOT EXISTS chat_sesiones_user_contrato_idx
  ON public.chat_sesiones (user_id, contrato_id, updated_at DESC);
