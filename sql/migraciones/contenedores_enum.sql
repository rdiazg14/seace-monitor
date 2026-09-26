-- =====================================================================
-- Contenedores no-PDF · aditivo idempotente · Fase 3.5
-- Amplía el enum de tdr_tipo_extraccion para aceptar los contenedores.
-- Seguro re-ejecutar. No toca chunks_tdr, embedding ni buscar_tdr.
-- Ejecutar en: Supabase → SQL Editor (o vía psycopg con DATABASE_URL).
-- =====================================================================

ALTER TABLE contratos DROP CONSTRAINT IF EXISTS contratos_tdr_tipo_extraccion_chk;
ALTER TABLE contratos ADD CONSTRAINT contratos_tdr_tipo_extraccion_chk
  CHECK (
    tdr_tipo_extraccion IS NULL
    OR tdr_tipo_extraccion IN (
      'nativo_puro', 'mixto', 'imagen_total',
      'contenedor_docx', 'contenedor_zip', 'contenedor_rar', 'contenedor_doc'
    )
  );

COMMENT ON COLUMN contratos.tdr_tipo_extraccion IS
  'nativo_puro | mixto | imagen_total | contenedor_docx | contenedor_zip | contenedor_rar | contenedor_doc. NULL si no hay PDF (sin_pdf).';

-- Tras ALTER, recarga el cache de PostgREST para que el RPC/select vea el DDL:
--   NOTIFY pgrst, 'reload schema';

SELECT conname, pg_get_constraintdef(oid) AS def
FROM pg_constraint
WHERE conname = 'contratos_tdr_tipo_extraccion_chk';
