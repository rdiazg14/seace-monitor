-- Capas fase 2: poblar clasificacion_contrato en paralelo.
-- IDEMPOTENTE: migraciones_datos.nombre='capas_fase2_poblar'.
-- NO toca contratos. Lectores siguen en contratos.categoria_it.
-- keyword_id queda NULL aqui: la cascada de HOY no es la historica;
-- se resuelve despues con scripts/capas_fase2_keyword_id.py (solo si
-- reproduce contratos.categoria_it).
--
-- Universo: categoria_it IS NOT NULL OR relevancia_ia IS NOT NULL (=4556).
-- Ejecutar: uv run python scripts/run_sql.py docs/capas_fase2_poblar.sql

BEGIN;

DO $$
DECLARE
  n_ins int := 0;
  n_tot int;
  n_gem int;
  n_kw int;
  n_uni int;
  ids_c1 bigint[] := ARRAY[
    273, 10353, 11435, 11988, 12399, 20626, 32171, 32378, 34382, 34492,
    35576, 35751, 36445, 36973, 40586, 43667, 46129, 50908, 55367, 57244,
    57871, 57882, 58672, 59934, 63954, 65580, 65997, 66279, 67658, 68477,
    70601, 70826, 72158, 72867, 74482, 77609, 77999, 79918, 84043, 85541,
    88126, 90076, 90342, 90815, 90819, 90832, 90869, 90875, 90891, 91148,
    91197, 91221, 91321, 91342
  ]::bigint[];
BEGIN
  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'capas_fase2_poblar'
  ) THEN
    SELECT count(*)::int INTO n_tot FROM clasificacion_contrato;
    RAISE NOTICE 'capas fase 2 ya aplicada. No-op. clasificacion_contrato=%', n_tot;
    RETURN;
  END IF;

  SELECT count(*)::int INTO n_uni
  FROM contratos
  WHERE categoria_it IS NOT NULL OR relevancia_ia IS NOT NULL;

  IF n_uni <> 4556 THEN
    RAISE EXCEPTION
      'capas_fase2_poblar: universo esperado 4556, hay %', n_uni;
  END IF;

  -- Guardia: los 54 C1 + 90331 deben existir con categoria_it.
  IF (
    SELECT count(*)::int FROM contratos
    WHERE id = ANY(ids_c1) AND categoria_it IS NOT NULL
  ) <> 54 THEN
    RAISE EXCEPTION 'capas_fase2_poblar: faltan C1 con categoria_it';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM contratos WHERE id = 90331 AND categoria_it IS NOT NULL
  ) THEN
    RAISE EXCEPTION 'capas_fase2_poblar: 90331 sin categoria_it';
  END IF;

  INSERT INTO clasificacion_contrato (
    contrato_id,
    categoria_it,
    relevancia_ia,
    capa,
    keyword_id,
    senal,
    senal_fuente,
    confianza,
    consenso_n,
    revisar,
    artefacto,
    creado_utc,
    actualizado_utc
  )
  SELECT
    c.id,
    c.categoria_it,
    c.relevancia_ia,
    CASE
      WHEN c.id = ANY(ids_c1) OR c.id = 90331 THEN 'gemini'
      ELSE 'keyword'
    END,
    NULL,  -- keyword_id: aproximacion posterior (script Python)
    NULL,
    NULL,
    NULL,
    CASE WHEN c.id = ANY(ids_c1) THEN 3 ELSE 0 END,
    (c.id = 90331),
    CASE
      WHEN c.id = ANY(ids_c1) THEN 'c1_consenso'
      WHEN c.id = 90331 THEN 'directo_1sep'
      ELSE 'backfill_keywords'
    END,
    now(),
    now()
  FROM contratos c
  WHERE c.categoria_it IS NOT NULL OR c.relevancia_ia IS NOT NULL
  ON CONFLICT (contrato_id) DO NOTHING;

  GET DIAGNOSTICS n_ins = ROW_COUNT;

  SELECT count(*)::int INTO n_tot FROM clasificacion_contrato;
  SELECT count(*)::int INTO n_gem
  FROM clasificacion_contrato WHERE capa = 'gemini';
  SELECT count(*)::int INTO n_kw
  FROM clasificacion_contrato WHERE capa = 'keyword';

  IF n_tot <> 4556 OR n_gem <> 55 OR n_kw <> 4501 THEN
    RAISE EXCEPTION
      'capas_fase2_poblar: conteos mal tot=% gem=% kw=% (esp 4556/55/4501)',
      n_tot, n_gem, n_kw;
  END IF;

  -- Diff inmediato contra contratos (categoria + relevancia).
  IF EXISTS (
    SELECT 1
    FROM contratos c
    JOIN clasificacion_contrato cl ON cl.contrato_id = c.id
    WHERE c.categoria_it IS DISTINCT FROM cl.categoria_it
       OR c.relevancia_ia IS DISTINCT FROM cl.relevancia_ia
  ) THEN
    RAISE EXCEPTION 'capas_fase2_poblar: diff categoria/relevancia vs contratos';
  END IF;

  INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
  VALUES (
    'capas_fase2_poblar',
    n_ins,
    jsonb_build_object(
      'universo', n_uni,
      'insertadas', n_ins,
      'gemini', n_gem,
      'keyword', n_kw,
      'keyword_id', 'null_en_sql_resolver_con_script'
    )
  );

  RAISE NOTICE
    'capas fase 2: insertadas=% total=% gemini=% keyword=%',
    n_ins, n_tot, n_gem, n_kw;
END $$;

COMMIT;
