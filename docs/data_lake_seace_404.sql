-- Data lake: marcar PDFs que SEACE ya no sirve (HTTP 404).
-- IDEMPOTENTE: ADD COLUMN IF NOT EXISTS + marcador.
-- pdf_storage_path sigue NULL. El backfill salta error='seace_404'.

BEGIN;

ALTER TABLE public.documentos
  ADD COLUMN IF NOT EXISTS error text;

COMMENT ON COLUMN public.documentos.error IS
  'seace_404 = el archivo ya no existe en SEACE. NULL = sin error conocido. No reintentar.';

CREATE INDEX IF NOT EXISTS documentos_error_idx
  ON public.documentos (error)
  WHERE error IS NOT NULL;

INSERT INTO public.migraciones_datos (nombre, filas_afectadas, detalle)
VALUES (
  'data_lake_seace_404',
  0,
  '{"col":"documentos.error","valor":"seace_404"}'::jsonb
)
ON CONFLICT (nombre) DO NOTHING;

COMMIT;
