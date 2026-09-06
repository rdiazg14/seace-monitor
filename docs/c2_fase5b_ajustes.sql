-- C2 fase 5b: sacar lo que costo mas de lo que aporto.
-- IDEMPOTENTE: migraciones_datos.nombre=c2_fase5b_ajustes.
--
-- Las excludes de obra existen como FILAS SEPARADAS en Hardware y en
-- Cloud/hosting (UNIQUE categoria+keyword+tipo). Este script solo toca
-- categoria='Hardware'. Cloud/hosting queda intacto.
-- puerta cortafuego / adecuacion del centro solo existen en Hardware.

BEGIN;

DO $$
DECLARE
  n_off int := 0;
  n_exc int := 0;
  n_chk int;
  n_cloud int;
BEGIN
  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'c2_fase5b_ajustes'
  ) THEN
    RAISE NOTICE 'C2 fase 5b ya aplicada. No-op.';
    RETURN;
  END IF;

  -- Guardia: Cloud/hosting debe seguir con sus excludes de obra activas.
  SELECT count(*)::int INTO n_cloud
  FROM it_keywords
  WHERE categoria = 'Cloud/hosting'
    AND tipo = 'excluye'
    AND activa
    AND keyword IN (
      'aire acondicionado', 'jardin', 'deteccion de humo', 'puesta a tierra',
      'fotovoltaico', 'aniego', 'grupo electrogeno', 'pozo a tierra',
      'subestacion', 'contraincendios'
    );
  IF n_cloud <> 10 THEN
    RAISE EXCEPTION
      'fase5b: Cloud/hosting deberia tener 10 excludes de obra activas (n=%)',
      n_cloud;
  END IF;

  -- 1.1 centro de computo + sus excludes de obra SOLO en Hardware.
  UPDATE it_keywords
     SET activa = false,
         nota = coalesce(nota || ' | ', '') ||
                'fase5b: centro de computo aporto 1 alta y sus excludes por categoria costaron 3 contratos de videovigilancia (67804, 56179, 43297). No compensa. Cloud/hosting conserva las suyas.'
   WHERE categoria = 'Hardware'
     AND (
       (tipo = 'incluye' AND keyword = 'centro de computo')
       OR (
         tipo = 'excluye'
         AND keyword IN (
           'aire acondicionado', 'jardin', 'deteccion de humo', 'puesta a tierra',
           'fotovoltaico', 'aniego', 'grupo electrogeno', 'pozo a tierra',
           'subestacion', 'contraincendios', 'puerta cortafuego',
           'adecuacion del centro'
         )
       )
     )
     AND activa;

  GET DIAGNOSTICS n_chk = ROW_COUNT;
  n_off := n_off + n_chk;

  IF n_chk <> 13 THEN
    RAISE EXCEPTION
      'fase5b: se esperaban 13 desactivaciones Hardware (1 include + 12 excludes), n=%',
      n_chk;
  END IF;

  -- 1.2 matricula en Desarrollo.
  UPDATE it_keywords
     SET activa = false,
         nota = coalesce(nota || ' | ', '') ||
                'fase5b: costo 44610 (software LoadView). El ruido que cortaba eran registros de matricula en plataforma virtual, 4 contratos. No compensa perder software legitimo.'
   WHERE categoria = 'Desarrollo software'
     AND tipo = 'excluye'
     AND keyword = 'matricula'
     AND activa;

  GET DIAGNOSTICS n_chk = ROW_COUNT;
  n_off := n_off + n_chk;
  IF n_chk <> 1 THEN
    RAISE EXCEPTION 'fase5b: matricula no desactivada (n=%)', n_chk;
  END IF;

  -- 1.3 tecnologia de la informacion en Soporte.
  UPDATE it_keywords
     SET activa = false,
         nota = coalesce(nota || ' | ', '') ||
                'fase5b: etiqueta por el AREA compradora, no por el objeto: la OTI comprando cables UTP o herramientas. Es exactamente lo que el prompt de C1 prohibe (clasificar por area, no por objeto).'
   WHERE categoria = 'Soporte tecnico'
     AND tipo = 'incluye'
     AND keyword = 'tecnologia de la informacion'
     AND activa;

  GET DIAGNOSTICS n_chk = ROW_COUNT;
  n_off := n_off + n_chk;
  IF n_chk <> 1 THEN
    RAISE EXCEPTION 'fase5b: tecnologia de la informacion no desactivada (n=%)', n_chk;
  END IF;

  -- 1.4 excludes cinta de reloj (consumible).
  INSERT INTO it_keywords (
    categoria, keyword, prioridad, tipo, limite_palabra, activa, tolera_plural, nota
  )
  SELECT
    'Hardware',
    v.keyword,
    p.prioridad,
    'excluye',
    false,
    true,
    false,
    'fase5b: reloj marcador traia consumibles (8439, 14872). Mismo patron que toner: el consumible no es el equipo.'
  FROM (VALUES
    ('cinta de reloj'),
    ('cinta para reloj')
  ) AS v(keyword)
  JOIN LATERAL (
    SELECT prioridad FROM it_keywords k
    WHERE k.categoria = 'Hardware'
    LIMIT 1
  ) p ON true
  ON CONFLICT (categoria, keyword, tipo) DO NOTHING;

  GET DIAGNOSTICS n_exc = ROW_COUNT;

  -- Cloud/hosting sigue con 10.
  SELECT count(*)::int INTO n_cloud
  FROM it_keywords
  WHERE categoria = 'Cloud/hosting'
    AND tipo = 'excluye'
    AND activa
    AND keyword IN (
      'aire acondicionado', 'jardin', 'deteccion de humo', 'puesta a tierra',
      'fotovoltaico', 'aniego', 'grupo electrogeno', 'pozo a tierra',
      'subestacion', 'contraincendios'
    );
  IF n_cloud <> 10 THEN
    RAISE EXCEPTION
      'fase5b: Cloud/hosting perdio excludes (n=%); abort', n_cloud;
  END IF;

  INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
  VALUES (
    'c2_fase5b_ajustes',
    n_off + n_exc,
    jsonb_build_object(
      'desactivadas', n_off,
      'excludes_nuevas', n_exc,
      'cloud_hosting_excludes_intactas', n_cloud
    )
  );

  RAISE NOTICE 'C2 fase 5b: off=% excludes_nuevas=% cloud_ok=%',
    n_off, n_exc, n_cloud;
END $$;

COMMIT;
