-- C2 fase 4c: reorden de prioridades + vocabulario OT.
-- NO toca contratos (el backfill sigue en --proponer).
-- IDEMPOTENTE: migraciones_datos.nombre=c2_fase4c_prioridades.
--
-- Licencias (8) sobre Desarrollo (9): "software" es substring de cualquier
-- formulacion de licencia y las exclusiones por frase no alcanzan
-- (79 contratos seguian cambiando: "licencia del software", "licencias software",
-- "suscripcion de licencia anual de software").
-- Telemetria (13) bajo Redes (11): una mencion secundaria de SCADA no debe
-- ganar sobre el objeto real (88083: videovigilancia).
--
-- Las 4 exclusiones de software de 4b se DESACTIVAN (activa=false), no se
-- borran: con Licencias delante ya no hacen falta y quedan como deuda
-- reversible.

BEGIN;

DO $$
DECLARE
  n_off int := 0;
  n_inc int := 0;
  n_ex int := 0;
  n_dup int := 0;
  n_gap int := 0;
  n_cats int := 0;
BEGIN
  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'c2_fase4c_prioridades'
  ) THEN
    RAISE NOTICE 'C2 fase 4c ya aplicada. No-op.';
    RETURN;
  END IF;

  -- 1.1 Reorden absoluto. No depende del valor actual; sin dups ni huecos.
  UPDATE it_keywords SET prioridad = CASE categoria
    WHEN 'Firma digital'       THEN 1
    WHEN 'IA/analytics'        THEN 2
    WHEN 'Ciberseguridad'      THEN 3
    WHEN 'Cloud/hosting'       THEN 4
    WHEN 'Microsoft'           THEN 5
    WHEN 'Oracle'              THEN 6
    WHEN 'Base de datos/ERP'   THEN 7
    WHEN 'Licencias'           THEN 8
    WHEN 'Desarrollo software' THEN 9
    WHEN 'Soporte tecnico'     THEN 10
    WHEN 'Redes/cableado'      THEN 11
    WHEN 'Correo electronico'  THEN 12
    WHEN 'Telemetria/OT'       THEN 13
    WHEN 'Hardware'            THEN 14
    ELSE prioridad
  END;

  SELECT count(DISTINCT categoria)::int INTO n_cats FROM it_keywords;

  SELECT count(*)::int INTO n_dup
  FROM (
    SELECT prioridad FROM it_keywords
    GROUP BY prioridad
    HAVING count(DISTINCT categoria) > 1
  ) d;

  SELECT count(*)::int INTO n_gap
  FROM generate_series(1, (SELECT max(prioridad) FROM it_keywords)) g(n)
  WHERE NOT EXISTS (SELECT 1 FROM it_keywords k WHERE k.prioridad = g.n);

  IF n_cats <> 14 OR n_dup <> 0 OR n_gap <> 0 THEN
    RAISE EXCEPTION 'fase4c: prioridad invalida cats=% dup=% gap=%',
      n_cats, n_dup, n_gap;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM it_keywords
    WHERE categoria = 'Licencias' AND prioridad = 8
  ) OR NOT EXISTS (
    SELECT 1 FROM it_keywords
    WHERE categoria = 'Desarrollo software' AND prioridad = 9
  ) OR NOT EXISTS (
    SELECT 1 FROM it_keywords
    WHERE categoria = 'Redes/cableado' AND prioridad = 11
  ) OR NOT EXISTS (
    SELECT 1 FROM it_keywords
    WHERE categoria = 'Telemetria/OT' AND prioridad = 13
  ) THEN
    RAISE EXCEPTION 'fase4c: reorden no coincidio con el mapa 8/9/11/13';
  END IF;

  -- 1.2 Deuda 4b: excludes de software ya no hacen falta.
  UPDATE it_keywords
     SET activa = false,
         nota = coalesce(nota || ' | ', '') ||
                'fase4c: desactivada; Licencias pri 8 ahora gana antes que software'
   WHERE categoria = 'Desarrollo software'
     AND tipo = 'excluye'
     AND activa
     AND keyword IN (
       'licencia de software',
       'licencias de software',
       'suscripcion de software',
       'suscripcion a licencia'
     );

  GET DIAGNOSTICS n_off = ROW_COUNT;

  -- 2. Includes OT (prioridad copiada de Telemetria/OT = 13).
  INSERT INTO it_keywords (
    categoria, keyword, prioridad, tipo, limite_palabra, activa, nota
  )
  SELECT
    'Telemetria/OT',
    v.keyword,
    p.prioridad,
    'incluye',
    v.limite_palabra,
    true,
    v.nota
  FROM (VALUES
    -- Volumen bajo, ruido nulo (medido 6 sep 2026)
    ('hmi', true,
     'fase4c: LP; 2/2 HMI para PLC'),
    ('iot', true,
     'fase4c: LP; 1/1 piloto meteo'),
    ('transmisor de nivel', false,
     'fase4c: 5/5 radar/agua'),
    ('variador de frecuencia', false,
     'fase4c: 1/1 VFD industrial'),
    ('variador de velocidad', false,
     'fase4c: 2/2 trifasico'),
    ('actuador', false,
     'fase4c: 2/2 neumaticos PTA'),
    ('arrancador suave', false,
     'fase4c: 2/2 motor industrial; exclude ventilador puede tumbar 76547'),
    ('pluviometro', false,
     'fase4c: 1/1 estacion hidrologica'),
    ('servomotor', false,
     'fase4c: 1/1; caso medido es inyector hidro'),
    ('electrovalvula', false,
     'fase4c: 7/7; exclude lavachatas'),
    ('macromedidor', false,
     'fase4c: 15; exclude camara electrica (frase pedida; puede no pegar camara de)'),
    -- Preventivos volumen 0
    ('raspberry', false,
     'fase4c: preventivo volumen 0'),
    ('arduino', false,
     'fase4c: preventivo volumen 0'),
    ('microcontrolador', false,
     'fase4c: preventivo volumen 0'),
    ('modbus', false,
     'fase4c: preventivo volumen 0'),
    ('opc ua', false,
     'fase4c: preventivo volumen 0'),
    ('mqtt', true,
     'fase4c: LP preventivo; token corto'),
    ('lorawan', false,
     'fase4c: preventivo volumen 0'),
    ('internet de las cosas', false,
     'fase4c: preventivo volumen 0'),
    ('estacion de bombeo automatizada', false,
     'fase4c: preventivo; no usa la palabra automatizacion (ruido >85%)'),
    -- Con exclusion
    ('sensor', false,
     'fase4c: con excludes medicos/automotriz/lab; ENERTRONIC no vende SpO2'),
    ('medidor de caudal', false,
     'fase4c: 42; excludes camara electrica / obra civil (frases pedidas)')
  ) AS v(keyword, limite_palabra, nota)
  JOIN LATERAL (
    SELECT prioridad FROM it_keywords k
    WHERE k.categoria = 'Telemetria/OT'
    LIMIT 1
  ) p ON true
  ON CONFLICT (categoria, keyword, tipo) DO NOTHING;

  GET DIAGNOSTICS n_inc = ROW_COUNT;

  -- 2. Excludes a nivel categoria (asi funciona it_keywords: un exclude
  -- tumba TODA Telemetria/OT si el texto lo contiene, no solo 'sensor').
  INSERT INTO it_keywords (
    categoria, keyword, prioridad, tipo, limite_palabra, activa, nota
  )
  SELECT
    'Telemetria/OT',
    v.keyword,
    p.prioridad,
    'excluye',
    false,
    true,
    v.nota
  FROM (VALUES
    ('saturacion',
     'fase4c: sensor SpO2 / medico'),
    ('gasto cardiaco',
     'fase4c: sensor transductor Vigileo'),
    ('ventilador',
     'fase4c: sensor de oxigeno medico; colateral: arrancador suave para ventilador'),
    ('anestesia',
     'fase4c: sensor BIS / SpO2'),
    ('oximetr',
     'fase4c: oximetro de pulso'),
    ('camioneta',
     'fase4c: sensores de admision automotriz'),
    ('vehiculo',
     'fase4c: alarma de auto; colateral posible con rastreo satelital de flota'),
    ('admision',
     'fase4c: sistema de admision de camionetas'),
    ('sensorial',
     'fase4c: cacao/cafe/ventana didactica/audifono, no sensor OT'),
    ('paciente',
     'fase4c: contexto medico'),
    ('quirurgic',
     'fase4c: monitor quirófano / SpO2'),
    ('biomedic',
     'fase4c: biomedico, no campo'),
    ('laparoscop',
     'fase4c: torre laparoscopica (hueco medido en residual sensor)'),
    ('arterial',
     'fase4c: transductor de linea arterial (hueco medido)'),
    ('alarma de auto',
     'fase4c: alarma con sensores para vehiculo'),
    ('balanza',
     'fase4c: calibracion de balanzas que mencionan sensor'),
    ('papel para scanner',
     'fase4c: sensor de avance de papel HP; el titulo medido dice escanner'),
    ('lavachatas',
     'fase4c: electrovalvula sanitaria, no OT'),
    ('camara electrica',
     'fase4c: pedida para medidor/macromedidor; titulos medidos dicen camara de'),
    ('obra civil',
     'fase4c: pedida para medidor de caudal; titulos medidos dicen obras/trabajos civiles')
  ) AS v(keyword, nota)
  JOIN LATERAL (
    SELECT prioridad FROM it_keywords k
    WHERE k.categoria = 'Telemetria/OT'
    LIMIT 1
  ) p ON true
  ON CONFLICT (categoria, keyword, tipo) DO NOTHING;

  GET DIAGNOSTICS n_ex = ROW_COUNT;

  INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
  VALUES (
    'c2_fase4c_prioridades',
    n_off + n_inc + n_ex,
    jsonb_build_object(
      'licencias', 8,
      'desarrollo', 9,
      'redes', 11,
      'telemetria', 13,
      'excludes_software_4b_off', n_off,
      'includes_ot', n_inc,
      'excludes_ot', n_ex,
      'nota_88083', 'videovigilancia con SCADA secundario queda Redes (pri 11 < 13)',
      'nota_73239', 'Licencias gana a software por prioridad, no por exclude'
    )
  );

  RAISE NOTICE 'C2 fase 4c: off4b=% includes=% excludes=% dup=% gap=%',
    n_off, n_inc, n_ex, n_dup, n_gap;
END $$;

COMMENT ON COLUMN it_keywords.prioridad IS
  '1..14. Primera categoria por prioridad que gana. 8=Licencias (antes de Desarrollo 9). 13=Telemetria/OT (NUCLEO; despues de Redes 11).';

COMMIT;
