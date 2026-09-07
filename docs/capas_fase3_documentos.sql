-- Capas fase 3: poblar documentos desde columnas pdf_* de contratos.
-- IDEMPOTENTE: migraciones_datos.nombre='capas_fase3_documentos'.
-- NO toca contratos. Una fila por contrato con pdf_archivo_id IS NOT NULL.
-- sha256 = pdf_hash (hash SHA-256 del binario, 64 hex).
-- bytes = pdf_storage_bytes (NULL si el PDF no esta en el bucket).
-- tipo_extraccion: nativo_puro→nativo, mixto→mixto, imagen_total→ocr.
--
-- Ejecutar: uv run python scripts/run_sql.py docs/capas_fase3_documentos.sql

BEGIN;

DO $$
DECLARE
  n_ins int := 0;
  n_tot int;
  n_src int;
  n_path int;
  n_path_src int;
BEGIN
  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'capas_fase3_documentos'
  ) THEN
    SELECT count(*)::int INTO n_tot FROM documentos;
    RAISE NOTICE 'capas fase 3 documentos ya aplicada. No-op. documentos=%', n_tot;
    RETURN;
  END IF;

  SELECT count(*)::int INTO n_src
  FROM contratos
  WHERE pdf_archivo_id IS NOT NULL;

  SELECT count(*)::int INTO n_path_src
  FROM contratos
  WHERE pdf_archivo_id IS NOT NULL
    AND pdf_storage_path IS NOT NULL
    AND btrim(pdf_storage_path) <> '';

  INSERT INTO documentos (
    contrato_id,
    pdf_archivo_id,
    storage_path,
    sha256,
    bytes,
    paginas,
    tipo_extraccion,
    descargado_utc
  )
  SELECT
    c.id,
    c.pdf_archivo_id,
    nullif(btrim(c.pdf_storage_path), ''),
    CASE
      WHEN c.pdf_hash IS NOT NULL
           AND length(btrim(c.pdf_hash)) = 64
        THEN lower(btrim(c.pdf_hash))
      ELSE NULL
    END,
    c.pdf_storage_bytes,
    c.tdr_n_paginas,
    CASE c.tdr_tipo_extraccion
      WHEN 'nativo_puro'  THEN 'nativo'
      WHEN 'mixto'        THEN 'mixto'
      WHEN 'imagen_total' THEN 'ocr'
      ELSE NULL
    END,
    coalesce(c.pdf_storage_at, now())
  FROM contratos c
  WHERE c.pdf_archivo_id IS NOT NULL;

  GET DIAGNOSTICS n_ins = ROW_COUNT;

  SELECT count(*)::int INTO n_tot FROM documentos;

  SELECT count(*)::int INTO n_path
  FROM documentos
  WHERE storage_path IS NOT NULL AND btrim(storage_path) <> '';

  IF n_tot <> n_src THEN
    RAISE EXCEPTION
      'capas_fase3_documentos: filas=% != contratos con pdf_archivo_id=%',
      n_tot, n_src;
  END IF;

  IF n_path <> n_path_src THEN
    RAISE EXCEPTION
      'capas_fase3_documentos: con storage_path=% != fuente=%',
      n_path, n_path_src;
  END IF;

  IF n_path <> 925 THEN
    RAISE EXCEPTION
      'capas_fase3_documentos: esperado 925 con storage_path (bucket), hay %',
      n_path;
  END IF;

  INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
  VALUES (
    'capas_fase3_documentos',
    n_ins,
    jsonb_build_object(
      'filas', n_tot,
      'con_storage_path', n_path,
      'fuente', 'contratos.pdf_*'
    )
  );

  RAISE NOTICE
    'capas fase 3 documentos OK. insertados=% con_path=%',
    n_ins, n_path;
END $$;

COMMIT;
