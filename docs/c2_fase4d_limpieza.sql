-- C2 fase 4d: quita keywords ambiguas de Telemetria/OT y limpia Licencias.
-- NO toca contratos (el backfill va aparte, --proponer / --aplicar).
-- IDEMPOTENTE: migraciones_datos.nombre=c2_fase4d_limpieza.
--
-- Hallazgo: los excludes son por CATEGORIA, no por keyword. 'vehiculo'
-- (puesto para sensores de camioneta) mataba 'rastreo satelital de vehiculos'.
-- No se puede filtrar el ruido de 'sensor' sin danar el resto de la categoria.
-- Esos casos los resuelve Gemini, que ve el contexto completo.

BEGIN;

DO $$
DECLARE
  n_kw int := 0;
  n_ex_ot int := 0;
  n_ex_lic int := 0;
  n_lav int := 0;
  n_plc int := 0;
BEGIN
  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'c2_fase4d_limpieza'
  ) THEN
    RAISE NOTICE 'C2 fase 4d ya aplicada. No-op.';
    RETURN;
  END IF;

  -- 1.1 Ambiguas: sensor (38 altas mezcladas) y medidor de caudal (33 SEDAPAR).
  UPDATE it_keywords
     SET activa = false,
         nota = coalesce(nota || ' | ', '') ||
                'fase4d: ambiguas: sensor trae 38 altas mezcladas (medico, automotriz, incendio, balanzas) y medidor de caudal trae 33 que son la campana de camaras electricas de SEDAPAR (obra civil). Las exclusiones son por categoria y danaban rastreo satelital y arrancador suave. Estos casos los resuelve Gemini, que ve el contexto completo.'
   WHERE categoria = 'Telemetria/OT'
     AND tipo = 'incluye'
     AND activa
     AND keyword IN ('sensor', 'medidor de caudal');

  GET DIAGNOSTICS n_kw = ROW_COUNT;

  -- 1.2 Todas las exclusiones de Telemetria/OT salvo lavachatas y protocolo plc.
  UPDATE it_keywords
     SET activa = false,
         nota = coalesce(nota || ' | ', '') ||
                'fase4d: exclude por categoria danaba otras keywords OT; desactivada'
   WHERE categoria = 'Telemetria/OT'
     AND tipo = 'excluye'
     AND activa
     AND keyword NOT IN ('lavachatas', 'protocolo plc');

  GET DIAGNOSTICS n_ex_ot = ROW_COUNT;

  SELECT count(*)::int INTO n_lav
  FROM it_keywords
  WHERE categoria = 'Telemetria/OT'
    AND tipo = 'excluye'
    AND keyword = 'lavachatas'
    AND activa;

  SELECT count(*)::int INTO n_plc
  FROM it_keywords
  WHERE categoria = 'Telemetria/OT'
    AND tipo = 'excluye'
    AND keyword = 'protocolo plc'
    AND activa;

  IF n_lav <> 1 OR n_plc <> 1 THEN
    RAISE EXCEPTION
      'fase4d: excludes que deben quedar activas: lavachatas=% protocolo plc=%',
      n_lav, n_plc;
  END IF;

  IF EXISTS (
    SELECT 1 FROM it_keywords
    WHERE categoria = 'Telemetria/OT'
      AND tipo = 'incluye'
      AND keyword IN ('sensor', 'medidor de caudal')
      AND activa
  ) THEN
    RAISE EXCEPTION 'fase4d: sensor o medidor de caudal siguen activas';
  END IF;

  -- 1.4 Excludes de Licencias (categoria no comparte keywords; exclude seguro).
  INSERT INTO it_keywords (
    categoria, keyword, prioridad, tipo, limite_palabra, activa, nota
  )
  SELECT
    'Licencias',
    v.keyword,
    p.prioridad,
    'excluye',
    false,
    true,
    'fase4d: ENERTRONIC vende licencias de software y servicios tecnologicos, no tramites municipales ni sanitarios. Medido en fase 3: ~2 de cada 10 de la keyword "licencia" eran de ese tipo.'
  FROM (VALUES
    ('licencia de funcionamiento'),
    ('licencia municipal'),
    ('licencia sanitaria'),
    ('licencia de conducir'),
    ('licencia de edificacion'),
    ('licencia de obra'),
    ('licencia con goce'),
    ('licencia sin goce')
  ) AS v(keyword)
  JOIN LATERAL (
    SELECT prioridad FROM it_keywords k
    WHERE k.categoria = 'Licencias'
    LIMIT 1
  ) p ON true
  ON CONFLICT (categoria, keyword, tipo) DO NOTHING;

  GET DIAGNOSTICS n_ex_lic = ROW_COUNT;

  INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
  VALUES (
    'c2_fase4d_limpieza',
    n_kw + n_ex_ot + n_ex_lic,
    jsonb_build_object(
      'includes_ot_off', n_kw,
      'excludes_ot_off', n_ex_ot,
      'excludes_licencias', n_ex_lic,
      'excludes_ot_quedan', jsonb_build_array('lavachatas', 'protocolo plc')
    )
  );

  RAISE NOTICE 'C2 fase 4d: kw_off=% ex_ot_off=% ex_lic=% lav=% plc=%',
    n_kw, n_ex_ot, n_ex_lic, n_lav, n_plc;
END $$;

COMMIT;
