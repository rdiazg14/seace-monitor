-- C2 fase 5: vocabulario limpio medido en auditoria de titulos.
-- TODAS las includes/excludes de abajo fueron aprobadas por Rolando.
-- NO entran: digitalizacion, gestion documental, tramite documentario,
-- procesamiento de datos (ruido de locacion de archivo; van a Gemini).
-- IDEMPOTENTE: migraciones_datos.nombre=c2_fase5_vocabulario.
-- prioridad se copia de la categoria ya cargada; no se inventa.
--
-- Pre-check medido 2026-09-06 (antes de insertar excludes):
--   Hardware actuales con texto de exclude de obra: 3 (<20, OK)
--   Soporte tecnico con 'capacitacion': 7 (<20, OK)

BEGIN;

DO $$
DECLARE
  n_inc int := 0;
  n_exc int := 0;
  n_plu int := 0;
  n_chk int;
BEGIN
  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'c2_fase5_vocabulario'
  ) THEN
    RAISE NOTICE 'C2 fase 5 ya aplicada (migraciones_datos.nombre=c2_fase5_vocabulario). No-op.';
    RETURN;
  END IF;

  -- 2. Plural inverso: singular + tolera_plural (no flag global).
  UPDATE it_keywords
     SET keyword = 'equipo de computo',
         tolera_plural = true,
         nota = coalesce(nota || ' | ', '') ||
                'fase5: singular + tolera_plural; gana 49 reales sin ruido nas/ups'
   WHERE categoria = 'Hardware'
     AND tipo = 'incluye'
     AND keyword = 'equipos de computo';

  GET DIAGNOSTICS n_chk = ROW_COUNT;
  n_plu := n_plu + n_chk;

  UPDATE it_keywords
     SET keyword = 'equipo informatico',
         tolera_plural = true,
         nota = coalesce(nota || ' | ', '') ||
                'fase5: singular + tolera_plural; gana 49 reales sin ruido nas/ups'
   WHERE categoria = 'Hardware'
     AND tipo = 'incluye'
     AND keyword = 'equipos informaticos';

  GET DIAGNOSTICS n_chk = ROW_COUNT;
  n_plu := n_plu + n_chk;

  IF n_plu <> 2 THEN
    RAISE EXCEPTION
      'fase5: se esperaban 2 UPDATEs de plural (equipo*); afectados=%', n_plu;
  END IF;

  SELECT count(*)::int INTO n_chk
  FROM it_keywords
  WHERE categoria = 'Hardware'
    AND tipo = 'incluye'
    AND keyword IN ('equipo de computo', 'equipo informatico')
    AND tolera_plural
    AND activa;

  IF n_chk <> 2 THEN
    RAISE EXCEPTION
      'fase5: equipo de computo / equipo informatico deben quedar con tolera_plural=true (n=%)',
      n_chk;
  END IF;

  -- 1. Includes nuevas.
  INSERT INTO it_keywords (
    categoria, keyword, prioridad, tipo, limite_palabra, activa, tolera_plural, nota
  )
  SELECT
    v.categoria,
    v.keyword,
    p.prioridad,
    'incluye',
    false,
    true,
    false,
    'fase5: auditoria de titulos; aprobada por Rolando'
  FROM (VALUES
    -- Redes/cableado
    ('Redes/cableado', 'telefonia'),
    ('Redes/cableado', 'telefono ip'),
    ('Redes/cableado', 'radio enlace'),
    ('Redes/cableado', 'radioenlace'),
    ('Redes/cableado', 'central telefonica'),
    -- Desarrollo software
    ('Desarrollo software', 'pagina web'),
    ('Desarrollo software', 'portal institucional'),
    ('Desarrollo software', 'intranet'),
    ('Desarrollo software', 'sistema de gestion documental'),
    ('Desarrollo software', 'aula virtual'),
    ('Desarrollo software', 'campus virtual'),
    ('Desarrollo software', 'plataforma virtual'),
    ('Desarrollo software', 'expediente electronico'),
    -- Hardware
    ('Hardware', 'reloj biometrico'),
    ('Hardware', 'reloj marcador'),
    ('Hardware', 'control de acceso biometrico'),
    ('Hardware', 'reconocimiento facial'),
    ('Hardware', 'lector de huella'),
    ('Hardware', 'centro de computo'),
    ('Hardware', 'equipo multifuncional'),
    -- Soporte tecnico
    ('Soporte tecnico', 'gobierno digital'),
    ('Soporte tecnico', 'transformacion digital'),
    ('Soporte tecnico', 'tecnologia de la informacion'),
    ('Soporte tecnico', 'microforma')
  ) AS v(categoria, keyword)
  JOIN LATERAL (
    SELECT prioridad FROM it_keywords k
    WHERE k.categoria = v.categoria
    LIMIT 1
  ) p ON true
  ON CONFLICT (categoria, keyword, tipo) DO NOTHING;

  GET DIAGNOSTICS n_inc = ROW_COUNT;

  -- 1. Excludes.
  INSERT INTO it_keywords (
    categoria, keyword, prioridad, tipo, limite_palabra, activa, tolera_plural, nota
  )
  SELECT
    v.categoria,
    v.keyword,
    p.prioridad,
    'excluye',
    false,
    true,
    false,
    v.nota
  FROM (VALUES
    -- Redes
    ('Redes/cableado', 'torre arriostrada',
     'fase5: medicion; obra civil / antenas, no TI'),
    ('Redes/cableado', 'arrendamiento de torre',
     'fase5: medicion; obra civil / antenas, no TI'),
    ('Redes/cableado', 'tramos de torre',
     'fase5: medicion; obra civil / antenas, no TI'),
    ('Redes/cableado', 'levantamiento de informacion en campo',
     'fase5: medicion; topografia/campo, no TI'),
    -- Desarrollo
    ('Desarrollo software', 'publicidad',
     'fase5: medicion; ruido de pagina web / portal'),
    ('Desarrollo software', 'diario',
     'fase5: medicion; ruido de pagina web / portal'),
    ('Desarrollo software', 'disfraz',
     'fase5: medicion; ruido de pagina web / portal'),
    ('Desarrollo software', 'merchandising',
     'fase5: medicion; ruido de pagina web / portal'),
    ('Desarrollo software', 'articulos promocionales',
     'fase5: medicion; ruido de pagina web / portal'),
    ('Desarrollo software', 'organizacion de eventos',
     'fase5: medicion; ruido de pagina web / portal'),
    ('Desarrollo software', 'chaleco',
     'fase5: medicion; ruido de pagina web / portal'),
    ('Desarrollo software', 'letreros',
     'fase5: medicion; ruido de pagina web / portal'),
    ('Desarrollo software', 'pases de visita',
     'fase5: medicion; ruido de pagina web / portal'),
    ('Desarrollo software', 'asistencia administrativa',
     'fase5: medicion; ruido de pagina web / portal'),
    ('Desarrollo software', 'matricula',
     'fase5: medicion; ruido de pagina web / portal'),
    ('Desarrollo software', 'spots radiales',
     'fase5: medicion; ruido de pagina web / portal'),
    -- Hardware: mismas 10 de obra de Cloud/hosting + 2 por centro de computo
    ('Hardware', 'aire acondicionado',
     'fase5: obra EN el centro de computo, no equipo TI'),
    ('Hardware', 'jardin',
     'fase5: obra EN el centro de computo, no equipo TI'),
    ('Hardware', 'deteccion de humo',
     'fase5: obra EN el centro de computo, no equipo TI'),
    ('Hardware', 'puesta a tierra',
     'fase5: obra EN el centro de computo, no equipo TI'),
    ('Hardware', 'fotovoltaico',
     'fase5: obra EN el centro de computo, no equipo TI'),
    ('Hardware', 'aniego',
     'fase5: obra EN el centro de computo, no equipo TI'),
    ('Hardware', 'grupo electrogeno',
     'fase5: obra EN el centro de computo, no equipo TI'),
    ('Hardware', 'pozo a tierra',
     'fase5: obra EN el centro de computo, no equipo TI'),
    ('Hardware', 'subestacion',
     'fase5: obra EN el centro de computo, no equipo TI'),
    ('Hardware', 'contraincendios',
     'fase5: obra EN el centro de computo, no equipo TI'),
    ('Hardware', 'puerta cortafuego',
     'fase5: obra EN el centro de computo (keyword nueva centro de computo)'),
    ('Hardware', 'adecuacion del centro',
     'fase5: obra EN el centro de computo (keyword nueva centro de computo)'),
    -- Soporte
    ('Soporte tecnico', 'actividades administrativas',
     'fase5: medicion; ruido de gobierno/transformacion digital'),
    ('Soporte tecnico', 'capacitacion',
     'fase5: medicion; ruido de gobierno/transformacion digital (pre-check: 7 Soporte actuales)'),
    ('Soporte tecnico', 'ponencia',
     'fase5: medicion; ruido de gobierno/transformacion digital'),
    ('Soporte tecnico', 'alquiler de mobiliario',
     'fase5: medicion; ruido de gobierno/transformacion digital'),
    ('Soporte tecnico', 'verificacion estructural',
     'fase5: medicion; ruido de gobierno/transformacion digital'),
    ('Soporte tecnico', 'arrendamiento de un inmueble',
     'fase5: medicion; ruido de gobierno/transformacion digital'),
    ('Soporte tecnico', 'analista administrativo',
     'fase5: medicion; ruido de gobierno/transformacion digital')
  ) AS v(categoria, keyword, nota)
  JOIN LATERAL (
    SELECT prioridad FROM it_keywords k
    WHERE k.categoria = v.categoria
    LIMIT 1
  ) p ON true
  ON CONFLICT (categoria, keyword, tipo) DO NOTHING;

  GET DIAGNOSTICS n_exc = ROW_COUNT;

  -- Guardias: no insertar las 4 sucias.
  IF EXISTS (
    SELECT 1 FROM it_keywords
    WHERE tipo = 'incluye'
      AND keyword IN (
        'digitalizacion',
        'gestion documental',
        'tramite documentario',
        'procesamiento de datos'
      )
      AND activa
      AND nota LIKE '%fase5%'
  ) THEN
    RAISE EXCEPTION 'fase5: no deben entrar las 4 keywords sucias';
  END IF;

  INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
  VALUES (
    'c2_fase5_vocabulario',
    n_inc + n_exc + n_plu,
    jsonb_build_object(
      'incluye', n_inc,
      'excluye', n_exc,
      'plural_a_singular', n_plu,
      'sin_digitalizacion', true,
      'sin_gestion_documental', true,
      'sin_tramite_documentario', true,
      'sin_procesamiento_de_datos', true,
      'precheck_hw_exclude_hit', 3,
      'precheck_soporte_capacitacion', 7
    )
  );

  RAISE NOTICE 'C2 fase 5: incluye=% excluye=% plural=%', n_inc, n_exc, n_plu;
END $$;

COMMIT;
