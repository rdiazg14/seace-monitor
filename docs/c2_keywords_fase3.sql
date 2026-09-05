-- C2 fase 3: 6 includes nuevas + 11 exclusiones en it_keywords.
-- NO cambia codigo de ingesta: ya lee la tabla (fase 2).
-- IDEMPOTENTE: si migraciones_datos.nombre=c2_keywords_fase3, no-op.
-- prioridad se copia de la categoria ya cargada; no se inventa.

BEGIN;

DO $$
DECLARE
  n int;
BEGIN
  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'c2_keywords_fase3'
  ) THEN
    RAISE NOTICE 'C2 fase 3 ya aplicada (migraciones_datos.nombre=c2_keywords_fase3). No-op.';
    RETURN;
  END IF;

  INSERT INTO it_keywords (
    categoria, keyword, prioridad, tipo, limite_palabra, activa, nota
  )
  SELECT
    v.categoria,
    v.keyword,
    p.prioridad,
    v.tipo,
    false,
    true,
    v.nota
  FROM (VALUES
    ('Ciberseguridad', 'antivirus', 'incluye',
     'fase3: 46 nulls, 0 ruido medido'),
    ('Redes/cableado', 'videovigilancia', 'incluye',
     'fase3: 43 nulls, camaras/NVR'),
    ('Redes/cableado', 'telefonia ip', 'incluye',
     'fase3: 11 nulls'),
    ('Redes/cableado', 'internet', 'incluye',
     'fase3: 344 nulls, conectividad'),
    ('Licencias', 'licencia', 'incluye',
     'fase3: requiere exclusion licenciad'),
    ('Cloud/hosting', 'data center', 'incluye',
     'fase3: requiere exclusiones de obra'),
    ('Licencias', 'licenciad', 'excluye',
     '329/570 de licencia eran licenciado/a (personal, no software)'),
    ('Cloud/hosting', 'aire acondicionado', 'excluye',
     'obra EN el data center, no infraestructura cloud'),
    ('Cloud/hosting', 'jardin', 'excluye',
     'obra EN el data center, no infraestructura cloud'),
    ('Cloud/hosting', 'deteccion de humo', 'excluye',
     'obra EN el data center, no infraestructura cloud'),
    ('Cloud/hosting', 'puesta a tierra', 'excluye',
     'obra EN el data center, no infraestructura cloud'),
    ('Cloud/hosting', 'fotovoltaico', 'excluye',
     'obra EN el data center, no infraestructura cloud'),
    ('Cloud/hosting', 'aniego', 'excluye',
     'obra EN el data center, no infraestructura cloud'),
    ('Hardware', 'toner', 'excluye',
     'consumible, no equipo. 230 filas a NULL: vuelven al pool y C1 las evalua con verificacion de senal'),
    ('Hardware', 'tinta', 'excluye',
     'consumible, no equipo. 230 filas a NULL: vuelven al pool y C1 las evalua con verificacion de senal'),
    ('Hardware', 'cartucho', 'excluye',
     'consumible, no equipo. 230 filas a NULL: vuelven al pool y C1 las evalua con verificacion de senal'),
    ('Hardware', 'cinta de impresion', 'excluye',
     'consumible, no equipo. 230 filas a NULL: vuelven al pool y C1 las evalua con verificacion de senal')
  ) AS v(categoria, keyword, tipo, nota)
  JOIN LATERAL (
    SELECT prioridad
    FROM it_keywords k
    WHERE k.categoria = v.categoria
    LIMIT 1
  ) p ON true
  ON CONFLICT (categoria, keyword, tipo) DO NOTHING;

  GET DIAGNOSTICS n = ROW_COUNT;

  INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
  VALUES (
    'c2_keywords_fase3',
    n,
    '{"incluye": 6, "excluye": 11, "sin_servidor": true, "sin_sistema_informatico": true}'::jsonb
  );

  RAISE NOTICE 'C2 fase 3 aplicada: filas_insertadas=%', n;
END $$;

COMMIT;
