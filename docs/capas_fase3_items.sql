-- Capas fase 3: poblar contrato_items desde contratos.items_json.
-- IDEMPOTENTE: migraciones_datos.nombre='capas_fase3_items'.
-- NO toca contratos. Sin FK a cubso_catalogo (dump 2016 incompleto).
-- Indices ya existen (fase 1): contrato_items_cod_cubso_idx.
--
-- Ejecutar: uv run python scripts/run_sql.py docs/capas_fase3_items.sql

BEGIN;

DO $$
DECLARE
  n_ins int := 0;
  n_tot int;
  n_ctr int;
  n_json_ctr int;
  n_json_items int;
BEGIN
  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'capas_fase3_items'
  ) THEN
    SELECT count(*)::int INTO n_tot FROM contrato_items;
    RAISE NOTICE 'capas fase 3 items ya aplicada. No-op. contrato_items=%', n_tot;
    RETURN;
  END IF;

  SELECT count(*)::int,
         coalesce(sum(jsonb_array_length(items_json)), 0)::int
    INTO n_json_ctr, n_json_items
  FROM contratos
  WHERE items_json IS NOT NULL
    AND jsonb_typeof(items_json) = 'array'
    AND jsonb_array_length(items_json) > 0;

  IF n_json_ctr <> 3977 OR n_json_items <> 7370 THEN
    RAISE EXCEPTION
      'capas_fase3_items: esperado 3977 contratos / 7370 items, hay % / %',
      n_json_ctr, n_json_items;
  END IF;

  INSERT INTO contrato_items (
    contrato_id,
    item_nro,
    cod_cubso,
    nom_cubso,
    descripcion,
    cantidad,
    unidad,
    distrito
  )
  SELECT
    c.id,
    ord.ordinality::int AS item_nro,
    nullif(ord.elem->>'cod_cubso', ''),
    nullif(ord.elem->>'nom_cubso', ''),
    nullif(ord.elem->>'descripcion', ''),
    CASE
      WHEN ord.elem->>'cantidad' IS NULL OR ord.elem->>'cantidad' = ''
        THEN NULL
      ELSE (ord.elem->>'cantidad')::numeric
    END,
    nullif(ord.elem->>'unidad', ''),
    nullif(ord.elem->>'distrito', '')
  FROM contratos c
  CROSS JOIN LATERAL jsonb_array_elements(c.items_json)
    WITH ORDINALITY AS ord(elem, ordinality)
  WHERE c.items_json IS NOT NULL
    AND jsonb_typeof(c.items_json) = 'array'
    AND jsonb_array_length(c.items_json) > 0;

  GET DIAGNOSTICS n_ins = ROW_COUNT;

  SELECT count(*)::int INTO n_tot FROM contrato_items;
  SELECT count(DISTINCT contrato_id)::int INTO n_ctr FROM contrato_items;

  IF n_tot <> n_json_items THEN
    RAISE EXCEPTION
      'capas_fase3_items: filas=% != items_json sum=%', n_tot, n_json_items;
  END IF;
  IF n_ctr <> n_json_ctr THEN
    RAISE EXCEPTION
      'capas_fase3_items: contratos distintos=% != json=%', n_ctr, n_json_ctr;
  END IF;

  INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
  VALUES (
    'capas_fase3_items',
    n_ins,
    jsonb_build_object(
      'contratos_con_items', n_ctr,
      'filas', n_tot,
      'fuente', 'contratos.items_json'
    )
  );

  RAISE NOTICE
    'capas fase 3 items OK. insertados=% contratos=% filas=%',
    n_ins, n_ctr, n_tot;
END $$;

COMMIT;
