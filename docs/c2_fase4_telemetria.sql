-- C2 fase 4: categoria Telemetria/OT, includes nuevas, plural selectivo.
-- NO cambia ingesta_completa.py: la ingesta ya lee it_keywords (fase 2).
-- tolera_plural: el backfill la consume; la ingesta todavia no (SELECT fijo).
-- IDEMPOTENTE: si migraciones_datos.nombre=c2_fase4_telemetria, no-op de datos.
--
-- Plural NO se activa en (medido 6 sep 2026, corpus 77963):
--   ' ups '                 147 FP (UPSS de hospital)
--   ' sap '                  11 FP (Sistema de Agua Potable)
--   ' erp '                   9  (Estaciones de Rastreo Permanente + S10-ERP)
--   ' tablet '                9  (sin validar; el testigo 92056 ya pega sin plural)
--   'sistema administrativo' 58 filas nuevas / 39 cambios de categoria sin validar

BEGIN;

ALTER TABLE it_keywords
  ADD COLUMN IF NOT EXISTS tolera_plural boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN it_keywords.tolera_plural IS
  'true = match con s/es opcional en CADA palabra (\\b palabrae?s? \\b ...). false = match actual (substring o limite_palabra).';

COMMENT ON COLUMN it_keywords.prioridad IS
  '1..14. Primera categoria por prioridad que gana. 3 = Telemetria/OT (NUCLEO).';

DO $$
DECLARE
  n_tel int := 0;
  n_kw int := 0;
  n_plu int := 0;
BEGIN
  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'c2_fase4_telemetria'
  ) THEN
    RAISE NOTICE 'C2 fase 4 ya aplicada (migraciones_datos.nombre=c2_fase4_telemetria). No-op.';
    RETURN;
  END IF;

  -- Correr 3..13 → 4..14. Un solo UPDATE: no hay UNIQUE(prioridad).
  UPDATE it_keywords
     SET prioridad = prioridad + 1
   WHERE prioridad >= 3;

  INSERT INTO it_keywords (
    categoria, keyword, prioridad, tipo, limite_palabra, activa, nota
  ) VALUES
    ('Telemetria/OT', 'telemetria', 3, 'incluye', false, true,
     'fase4: nucleo CRITERIOS; 7/7 nulls medidos'),
    ('Telemetria/OT', 'datalogger', 3, 'incluye', false, true,
     'fase4: 18/18 nulls'),
    ('Telemetria/OT', 'data logger', 3, 'incluye', false, true,
     'fase4: variante de datalogger'),
    ('Telemetria/OT', 'plc', 3, 'incluye', true, true,
     'fase4: LP; 11/11 nulls; exclude protocolo plc'),
    ('Telemetria/OT', 'rtu', 3, 'incluye', true, true,
     'fase4: LP; 1 null (SCADA EGESUR)'),
    ('Telemetria/OT', 'scada', 3, 'incluye', true, true,
     'fase4: LP para no pegar cascada'),
    ('Telemetria/OT', 'caudalimetro', 3, 'incluye', false, true,
     'fase4: 3/3 nulls'),
    ('Telemetria/OT', 'estacion meteorologica', 3, 'incluye', false, true,
     'fase4: 3/3 nulls'),
    ('Telemetria/OT', 'estacion hidrologica', 3, 'incluye', false, true,
     'fase4: 1/1 nulls'),
    ('Telemetria/OT', 'rastreo satelital', 3, 'incluye', false, true,
     'fase4: 5/5 nulls, flota'),
    ('Telemetria/OT', 'protocolo plc', 3, 'excluye', false, true,
     'fase4: powerline, no controlador logico. Caso 27198')
  ON CONFLICT (categoria, keyword, tipo) DO NOTHING;

  GET DIAGNOSTICS n_tel = ROW_COUNT;

  INSERT INTO it_keywords (
    categoria, keyword, prioridad, tipo, limite_palabra, activa, nota
  )
  SELECT
    v.categoria,
    v.keyword,
    p.prioridad,
    v.tipo,
    v.limite_palabra,
    true,
    v.nota
  FROM (VALUES
    ('Hardware', 'hardware', false, 'incluye',
     'fase4: 20 nulls, 28 total; palabra limpia'),
    ('Hardware', 'storage', false, 'incluye',
     'fase4: 13 nulls; cuidado BESS lo corta el exclude de software en otra cat'),
    ('Hardware', 'nas', true, 'incluye',
     'fase4: LP; 23 nulls'),
    ('Redes/cableado', 'videoconferencia', false, 'incluye',
     'fase4: 60 nulls'),
    ('Desarrollo software', 'sistema informatic', false, 'incluye',
     'fase4: 12 nulls; fase 3 descarto sistema informatico por locacion'),
    ('Desarrollo software', 'sistema de planillas', false, 'incluye',
     'fase4: 4/4 nulls'),
    ('Desarrollo software', 'software', false, 'incluye',
     'fase4: 173 nulls; requiere excludes bachiller/locacion/BESS'),
    ('Base de datos/ERP', 'plataforma gis', false, 'incluye',
     'fase4: 11 nulls, catastro GIS'),
    ('Desarrollo software', 'bachiller', false, 'excluye',
     'fase4: locacion de personal (ing. de software)'),
    ('Desarrollo software', 'locacion de servicio', false, 'excluye',
     'fase4: locacion de personal, no desarrollo'),
    ('Desarrollo software', 'battery energy storage', false, 'excluye',
     'fase4: BESS (68881), no software')
  ) AS v(categoria, keyword, limite_palabra, tipo, nota)
  JOIN LATERAL (
    SELECT prioridad
    FROM it_keywords k
    WHERE k.categoria = v.categoria
    LIMIT 1
  ) p ON true
  ON CONFLICT (categoria, keyword, tipo) DO NOTHING;

  GET DIAGNOSTICS n_kw = ROW_COUNT;

  UPDATE it_keywords
     SET tolera_plural = true,
         nota = coalesce(nota || ' | ', '') ||
                'fase4: tolera_plural (s/es en cualquier palabra)'
   WHERE tipo = 'incluye'
     AND keyword IN (
       'licencia de software',
       'sistema de informacion',
       'disco duro',
       'base de datos',
       'correo electronico'
     )
     AND NOT tolera_plural;

  GET DIAGNOSTICS n_plu = ROW_COUNT;

  INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
  VALUES (
    'c2_fase4_telemetria',
    n_tel + n_kw + n_plu,
    jsonb_build_object(
      'categoria_nueva', 'Telemetria/OT',
      'prioridad', 3,
      'shift', '3..13 -> 4..14',
      'insert_telemetria', n_tel,
      'insert_resto', n_kw,
      'tolera_plural', n_plu,
      'sin_sensor_usb_informatic_gps', true
    )
  );

  RAISE NOTICE 'C2 fase 4 aplicada: plural_flags=%', n_plu;
END $$;

COMMIT;
