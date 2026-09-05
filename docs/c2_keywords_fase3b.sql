-- C2 fase 3b: exclusiones de planta fisica en Cloud/hosting.
-- IDEMPOTENTE: si migraciones_datos.nombre=c2_keywords_fase3b, no-op.
-- prioridad se copia de Cloud/hosting ya cargada; no se inventa.

BEGIN;

DO $$
DECLARE
  n int;
BEGIN
  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'c2_keywords_fase3b'
  ) THEN
    RAISE NOTICE 'C2 fase 3b ya aplicada (migraciones_datos.nombre=c2_keywords_fase3b). No-op.';
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
    ('Cloud/hosting', 'grupo electrogeno', 'excluye',
     'fase3b: planta fisica DEL data center, no infraestructura cloud. Caso 85729: mantenimiento de grupo electrogeno movia Soporte tecnico -> Cloud/hosting'),
    ('Cloud/hosting', 'pozo a tierra', 'excluye',
     'fase3b: planta fisica DEL data center, no infraestructura cloud. Caso 85729: mantenimiento de grupo electrogeno movia Soporte tecnico -> Cloud/hosting'),
    ('Cloud/hosting', 'subestacion', 'excluye',
     'fase3b: planta fisica DEL data center, no infraestructura cloud. Caso 85729: mantenimiento de grupo electrogeno movia Soporte tecnico -> Cloud/hosting'),
    ('Cloud/hosting', 'contraincendios', 'excluye',
     'fase3b: planta fisica DEL data center, no infraestructura cloud. Caso 85729: mantenimiento de grupo electrogeno movia Soporte tecnico -> Cloud/hosting')
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
    'c2_keywords_fase3b',
    n,
    '{"excluye": 4, "categoria": "Cloud/hosting"}'::jsonb
  );

  RAISE NOTICE 'C2 fase 3b aplicada: filas_insertadas=%', n;
END $$;

COMMIT;
