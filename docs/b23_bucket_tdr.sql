-- B23: cache de PDFs. Hoy descargar_requerimiento.py baja el PDF a un
-- tempfile, extrae el texto y lo borra. El front manda al usuario a la
-- ficha SEACE, que es lenta y obliga a buscar el anexo a mano.
-- Guardar el binario permite abrirlo directo. Bucket privado + signed URL:
-- son documentos publicos pero no queremos hotlinking ni indexacion.
--
-- IDEMPOTENTE: si migraciones_datos.nombre=b23_bucket_tdr, no-op.
--
-- Ruta en el bucket: tdr/{contrato_id}/{pdf_archivo_id}.pdf
-- Predecible desde la fila (id + pdf_archivo_id), sin listar Storage.
--
-- Lectura: authenticated, solo bucket_id = 'tdr' (patron cotizar_tipo_log:
-- GRANT SELECT + POLICY FOR SELECT TO authenticated).
-- Escritura: sin policy INSERT/UPDATE/DELETE → solo service_role.

DO $b23$
BEGIN
  CREATE TABLE IF NOT EXISTS migraciones_datos (
    nombre          text PRIMARY KEY,
    aplicada_utc    timestamptz NOT NULL DEFAULT now(),
    filas_afectadas int,
    detalle         jsonb
  );

  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'b23_bucket_tdr'
  ) THEN
    RAISE NOTICE 'B23 ya aplicada (migraciones_datos.nombre=b23_bucket_tdr). No-op.';
    RETURN;
  END IF;

  ALTER TABLE public.contratos
    ADD COLUMN IF NOT EXISTS pdf_storage_path text;
  ALTER TABLE public.contratos
    ADD COLUMN IF NOT EXISTS pdf_storage_at timestamptz;
  ALTER TABLE public.contratos
    ADD COLUMN IF NOT EXISTS pdf_storage_bytes int;

  EXECUTE 'COMMENT ON COLUMN public.contratos.pdf_storage_path IS ''Ruta en bucket tdr: tdr/{contrato_id}/{pdf_archivo_id}.pdf. NULL si no cacheado.''';
  EXECUTE 'COMMENT ON COLUMN public.contratos.pdf_storage_at IS ''Cuando se subio el PDF a Storage.''';
  EXECUTE 'COMMENT ON COLUMN public.contratos.pdf_storage_bytes IS ''Tamano del PDF cacheado, en bytes.''';

  IF NOT EXISTS (SELECT 1 FROM storage.buckets WHERE id = 'tdr') THEN
    INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
    VALUES ('tdr', 'tdr', false, 52428800, ARRAY['application/pdf']::text[]);
    RAISE NOTICE 'B23: bucket tdr creado';
  ELSE
    RAISE NOTICE 'B23: bucket tdr ya existia';
  END IF;

  EXECUTE 'GRANT SELECT ON storage.objects TO authenticated';
  EXECUTE 'DROP POLICY IF EXISTS tdr_select_authenticated ON storage.objects';
  EXECUTE $p$
    CREATE POLICY tdr_select_authenticated
      ON storage.objects
      FOR SELECT
      TO authenticated
      USING (bucket_id = 'tdr')
  $p$;
  EXECUTE $p$
    COMMENT ON POLICY tdr_select_authenticated ON storage.objects IS
      'Lectura authenticated del bucket tdr. Escritura solo service_role (sin policy INSERT).'
  $p$;

  INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
  VALUES (
    'b23_bucket_tdr',
    0,
    jsonb_build_object(
      'bucket', 'tdr',
      'public', false,
      'file_size_limit', 52428800,
      'ruta', 'tdr/{contrato_id}/{pdf_archivo_id}.pdf'
    )
  );

  EXECUTE 'NOTIFY pgrst, ''reload schema''';
  RAISE NOTICE 'B23 aplicada: bucket tdr privado, columnas pdf_storage_*';
END
$b23$;
