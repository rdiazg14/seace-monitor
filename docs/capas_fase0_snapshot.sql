-- Capas fase 0: snapshot de etiquetas ANTES de separar inferencia de contratos.
-- Red para no perder las ~4213 categoria_it (C1, 90331, keywords) si algo
-- sale mal. Patron C2 (categoria_it_snapshot_c2).
-- IDEMPOTENTE: si migraciones_datos.nombre=capas_fase0_snapshot, no-op.
-- ON CONFLICT DO NOTHING: no pisa un snapshot ya capturado.
-- NO toca contratos. NO crea clasificacion_contrato. NO es fase 2.
--
-- Ejecutar: uv run python scripts/run_sql.py docs/capas_fase0_snapshot.sql

BEGIN;

CREATE TABLE IF NOT EXISTS migraciones_datos (
  nombre          text PRIMARY KEY,
  aplicada_utc    timestamptz NOT NULL DEFAULT now(),
  filas_afectadas int,
  detalle         jsonb
);

CREATE TABLE IF NOT EXISTS categoria_it_snapshot_capas (
  id                   bigint PRIMARY KEY,
  categoria_it_antes   text,
  relevancia_ia_antes  text,
  capturado_utc        timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE categoria_it_snapshot_capas IS
  'Capas fase 0: categoria_it y relevancia_ia al momento de partir inferencia. No se reescribe.';

ALTER TABLE categoria_it_snapshot_capas ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON TABLE public.categoria_it_snapshot_capas TO authenticated;

DROP POLICY IF EXISTS snapshot_capas_select_admin ON public.categoria_it_snapshot_capas;
CREATE POLICY snapshot_capas_select_admin
  ON public.categoria_it_snapshot_capas
  FOR SELECT
  TO authenticated
  USING (public.es_admin());

DO $$
DECLARE
  n int;
  n_cat int;
  n_ia int;
  n_solo_ia int;
  n_c1 int;
  n_90331 int;
BEGIN
  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'capas_fase0_snapshot'
  ) THEN
    SELECT
      count(*),
      count(*) FILTER (WHERE categoria_it_antes IS NOT NULL),
      count(*) FILTER (WHERE relevancia_ia_antes IS NOT NULL),
      count(*) FILTER (WHERE categoria_it_antes IS NULL AND relevancia_ia_antes IS NOT NULL)
    INTO n, n_cat, n_ia, n_solo_ia
    FROM categoria_it_snapshot_capas;
    RAISE NOTICE 'capas fase 0 ya aplicada (migraciones_datos.nombre=capas_fase0_snapshot). No-op. snapshot_filas=% categoria_it=% relevancia_ia=% solo_ia=%',
      n, n_cat, n_ia, n_solo_ia;
    RETURN;
  END IF;

  INSERT INTO categoria_it_snapshot_capas (
    id, categoria_it_antes, relevancia_ia_antes
  )
  SELECT id, categoria_it, relevancia_ia
  FROM contratos
  WHERE categoria_it IS NOT NULL
     OR relevancia_ia IS NOT NULL
  ON CONFLICT (id) DO NOTHING;

  GET DIAGNOSTICS n = ROW_COUNT;

  SELECT
    count(*) FILTER (WHERE categoria_it_antes IS NOT NULL),
    count(*) FILTER (WHERE relevancia_ia_antes IS NOT NULL),
    count(*) FILTER (WHERE categoria_it_antes IS NULL AND relevancia_ia_antes IS NOT NULL),
    count(*) FILTER (WHERE id IN (
      273, 10353, 11435, 11988, 12399, 20626, 32171, 32378, 34382, 34492,
      35576, 35751, 36445, 36973, 40586, 43667, 46129, 50908, 55367, 57244,
      57871, 57882, 58672, 59934, 63954, 65580, 65997, 66279, 67658, 68477,
      70601, 70826, 72158, 72867, 74482, 77609, 77999, 79918, 84043, 85541,
      88126, 90076, 90342, 90815, 90819, 90832, 90869, 90875, 90891, 91148,
      91197, 91221, 91321, 91342
    )),
    count(*) FILTER (WHERE id = 90331)
  INTO n_cat, n_ia, n_solo_ia, n_c1, n_90331
  FROM categoria_it_snapshot_capas;

  INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
  VALUES (
    'capas_fase0_snapshot',
    n,
    jsonb_build_object(
      'tabla', 'categoria_it_snapshot_capas',
      'filtro', 'categoria_it IS NOT NULL OR relevancia_ia IS NOT NULL',
      'filas', n,
      'categoria_it', n_cat,
      'relevancia_ia', n_ia,
      'solo_ia', n_solo_ia,
      'c1_54', n_c1,
      'id_90331', n_90331
    )
  );

  RAISE NOTICE 'capas fase 0 snapshot: insertadas=% categoria_it=% relevancia_ia=% solo_ia=% c1_54=% id_90331=%',
    n, n_cat, n_ia, n_solo_ia, n_c1, n_90331;
END $$;

COMMIT;
