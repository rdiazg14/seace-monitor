-- Capas fase 6: snapshot previo al DROP de categoria_it/relevancia_ia.
-- IDEMPOTENTE: migraciones_datos.nombre='capas_fase6_snapshot'.
-- Es la unica vuelta atras: restaura las columnas en contratos.

DO $$
DECLARE
  n_snap int;
BEGIN
  CREATE TABLE IF NOT EXISTS migraciones_datos (
    nombre          text PRIMARY KEY,
    aplicada_utc    timestamptz NOT NULL DEFAULT now(),
    filas_afectadas int,
    detalle         jsonb
  );

  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'capas_fase6_snapshot'
  ) THEN
    SELECT count(*)::int INTO n_snap
    FROM categoria_it_snapshot_capas_fase6;
    RAISE NOTICE 'capas_fase6_snapshot ya aplicada. No-op. filas=%', n_snap;
    RETURN;
  END IF;

  CREATE TABLE IF NOT EXISTS categoria_it_snapshot_capas_fase6 (
    id                   bigint PRIMARY KEY,
    categoria_it_antes   text,
    relevancia_ia_antes  text,
    capturado_utc        timestamptz NOT NULL DEFAULT now()
  );

  COMMENT ON TABLE categoria_it_snapshot_capas_fase6 IS
    'Capas fase 6: categoria_it y relevancia_ia en contratos justo antes del DROP. Permite recrearlas.';

  TRUNCATE categoria_it_snapshot_capas_fase6;

  INSERT INTO categoria_it_snapshot_capas_fase6
    (id, categoria_it_antes, relevancia_ia_antes)
  SELECT id, categoria_it, relevancia_ia
  FROM contratos
  WHERE categoria_it IS NOT NULL OR relevancia_ia IS NOT NULL;

  GET DIAGNOSTICS n_snap = ROW_COUNT;

  INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
  VALUES (
    'capas_fase6_snapshot',
    n_snap,
    jsonb_build_object(
      'tabla', 'categoria_it_snapshot_capas_fase6',
      'origen', 'contratos.categoria_it / contratos.relevancia_ia',
      'criterio', 'filas con categoria_it o relevancia_ia no nula'
    )
  );

  RAISE NOTICE 'capas_fase6_snapshot aplicada: filas=%', n_snap;
END;
$$;
