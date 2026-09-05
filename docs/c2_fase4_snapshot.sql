-- C2 fase 4: snapshot de categoria_it ANTES del backfill.
-- El backfill desetiqueta ~222 filas (consumibles Hardware). Sin esta
-- tabla no hay forma de saber que tenian ni de revertir. Esta tabla es la red.
-- IDEMPOTENTE: si migraciones_datos.nombre=c2_fase4_snapshot, no-op.
-- ON CONFLICT DO NOTHING: no pisa un snapshot ya capturado.

BEGIN;

CREATE TABLE IF NOT EXISTS migraciones_datos (
  nombre          text PRIMARY KEY,
  aplicada_utc    timestamptz NOT NULL DEFAULT now(),
  filas_afectadas int,
  detalle         jsonb
);

CREATE TABLE IF NOT EXISTS categoria_it_snapshot_c2 (
  id                   bigint PRIMARY KEY,
  categoria_it_antes   text,
  relevancia_ia_antes  text,
  capturado_utc        timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE categoria_it_snapshot_c2 IS
  'C2 fase 4: categoria_it (y relevancia_ia) de filas ya etiquetadas, tomado antes del backfill. Permite revertir desetiquetados.';

DO $$
DECLARE
  n int;
BEGIN
  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'c2_fase4_snapshot'
  ) THEN
    RAISE NOTICE 'C2 fase 4 snapshot ya aplicado (migraciones_datos.nombre=c2_fase4_snapshot). No-op.';
    RETURN;
  END IF;

  INSERT INTO categoria_it_snapshot_c2 (
    id, categoria_it_antes, relevancia_ia_antes
  )
  SELECT id, categoria_it, relevancia_ia
  FROM contratos
  WHERE categoria_it IS NOT NULL
  ON CONFLICT (id) DO NOTHING;

  GET DIAGNOSTICS n = ROW_COUNT;

  INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
  VALUES (
    'c2_fase4_snapshot',
    n,
    '{"tabla": "categoria_it_snapshot_c2", "filtro": "categoria_it IS NOT NULL"}'::jsonb
  );

  RAISE NOTICE 'C2 fase 4 snapshot: filas_capturadas=%', n;
END $$;

COMMIT;
