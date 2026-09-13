-- Historial de corridas del pipeline (append-only, una fila por corrida/paso).
-- Cubre los logs que antes vivían en data/ultima_*.txt (ingesta, OCR, PDF,
-- capas) y se sobrescribían en cada corrida, perdiendo la historia.
-- Escritura: service role (scripts del pipeline). Lectura: solo admin
-- (es_admin), igual que cotizar_tipo_log. No se expone al anon del front.
CREATE TABLE IF NOT EXISTS public.pipeline_runs (
    id      bigserial PRIMARY KEY,
    paso    text NOT NULL,             -- 'ingesta' | 'ocr' | 'pdf' | 'capas'
    run_id  text,                      -- GITHUB_RUN_ID (une con Actions)
    ts      timestamptz NOT NULL DEFAULT now(),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS pipeline_runs_paso_ts_idx
  ON public.pipeline_runs (paso, ts DESC);

CREATE INDEX IF NOT EXISTS pipeline_runs_payload_gin
  ON public.pipeline_runs USING GIN (payload);

ALTER TABLE public.pipeline_runs ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.pipeline_runs FROM anon, authenticated;
GRANT SELECT ON TABLE public.pipeline_runs TO authenticated;

DROP POLICY IF EXISTS pipeline_runs_select_admin ON public.pipeline_runs;
CREATE POLICY pipeline_runs_select_admin
  ON public.pipeline_runs
  FOR SELECT TO authenticated
  USING (public.es_admin());

COMMENT ON TABLE public.pipeline_runs IS
  'Historial append-only de corridas del pipeline. payload = stats k=v de cada paso.';

NOTIFY pgrst, 'reload schema';
