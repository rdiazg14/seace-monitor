-- Chat: sesiones persistentes + mensajes + uso de tokens (Fase 1).
--
-- Persistencia del asistente SEACE. Cada usuario (auth.users) tiene N sesiones
-- y cada sesión N mensajes. Los tokens (prompt/completion) por mensaje los
-- reporta el Worker desde el usageMetadata de Gemini; la suma por sesión vive
-- desnormalizada en chat_sesiones para pintar la lista sin leer mensajes.
--
-- RLS: user_id = auth.uid() en ambas tablas. El front escribe con el JWT del
-- usuario logueado; no se usa service_role.

CREATE TABLE IF NOT EXISTS public.chat_sesiones (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id           uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  titulo            text NOT NULL DEFAULT 'Nueva conversación',
  tokens_prompt     bigint NOT NULL DEFAULT 0,
  tokens_completion bigint NOT NULL DEFAULT 0,
  n_mensajes        int    NOT NULL DEFAULT 0,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.chat_mensajes (
  id                bigserial PRIMARY KEY,
  sesion_id         uuid NOT NULL REFERENCES public.chat_sesiones(id) ON DELETE CASCADE,
  user_id           uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  rol               text NOT NULL CHECK (rol IN ('user','bot')),
  texto             text NOT NULL,
  refs              jsonb,
  tokens_prompt     int NOT NULL DEFAULT 0,
  tokens_completion int NOT NULL DEFAULT 0,
  error             boolean NOT NULL DEFAULT false,
  limit_flag        boolean NOT NULL DEFAULT false,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chat_mensajes_sesion_idx ON public.chat_mensajes (sesion_id, id);
CREATE INDEX IF NOT EXISTS chat_sesiones_user_idx ON public.chat_sesiones (user_id, updated_at DESC);

ALTER TABLE public.chat_sesiones ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.chat_mensajes ENABLE ROW LEVEL SECURITY;

-- ── Sesiones ─────────────────────────────────────────────────────────────
DROP POLICY IF EXISTS chat_sesiones_select ON public.chat_sesiones;
CREATE POLICY chat_sesiones_select ON public.chat_sesiones
  FOR SELECT USING (user_id = auth.uid());

DROP POLICY IF EXISTS chat_sesiones_insert ON public.chat_sesiones;
CREATE POLICY chat_sesiones_insert ON public.chat_sesiones
  FOR INSERT WITH CHECK (user_id = auth.uid());

DROP POLICY IF EXISTS chat_sesiones_update ON public.chat_sesiones;
CREATE POLICY chat_sesiones_update ON public.chat_sesiones
  FOR UPDATE USING (user_id = auth.uid());

DROP POLICY IF EXISTS chat_sesiones_delete ON public.chat_sesiones;
CREATE POLICY chat_sesiones_delete ON public.chat_sesiones
  FOR DELETE USING (user_id = auth.uid());

-- ── Mensajes ─────────────────────────────────────────────────────────────
DROP POLICY IF EXISTS chat_mensajes_select ON public.chat_mensajes;
CREATE POLICY chat_mensajes_select ON public.chat_mensajes
  FOR SELECT USING (user_id = auth.uid());

DROP POLICY IF EXISTS chat_mensajes_insert ON public.chat_mensajes;
CREATE POLICY chat_mensajes_insert ON public.chat_mensajes
  FOR INSERT WITH CHECK (user_id = auth.uid());

DROP POLICY IF EXISTS chat_mensajes_delete ON public.chat_mensajes;
CREATE POLICY chat_mensajes_delete ON public.chat_mensajes
  FOR DELETE USING (user_id = auth.uid());

GRANT SELECT, INSERT, UPDATE, DELETE ON public.chat_sesiones TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.chat_mensajes TO authenticated;
GRANT USAGE, SELECT ON SEQUENCE public.chat_mensajes_id_seq TO authenticated;
