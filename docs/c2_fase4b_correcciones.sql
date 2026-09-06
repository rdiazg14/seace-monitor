-- C2 fase 4b: excludes de software vs Licencias, Telemetria/OT a pri 11,
-- seace_rubro_linea + Telemetria/OT -> nucleo.
-- IDEMPOTENTE: migraciones_datos.nombre=c2_fase4b_correcciones.

BEGIN;

DO $$
DECLARE
  n_ex int := 0;
  n_dup int := 0;
  n_gap int := 0;
BEGIN
  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'c2_fase4b_correcciones'
  ) THEN
    RAISE NOTICE 'C2 fase 4b ya aplicada. No-op.';
    RETURN;
  END IF;

  -- 1.1 software le robaba Licencias (226 cambios). Pri 9 vs 10.
  INSERT INTO it_keywords (
    categoria, keyword, prioridad, tipo, limite_palabra, activa, nota
  )
  SELECT
    v.categoria,
    v.keyword,
    p.prioridad,
    'excluye',
    false,
    true,
    v.nota
  FROM (VALUES
    ('Desarrollo software', 'licencia de software',
     'fase4b: software es substring de "licencia de software"; sin esto, Desarrollo (pri 9) le robaba 226 contratos a Licencias (pri 10): Office, Adobe, MS Project, BI'),
    ('Desarrollo software', 'licencias de software',
     'fase4b: plural; software es substring de "licencia de software"; sin esto, Desarrollo (pri 9) le robaba 226 contratos a Licencias (pri 10): Office, Adobe, MS Project, BI'),
    ('Desarrollo software', 'suscripcion de software',
     'fase4b: software es substring de "licencia de software"; sin esto, Desarrollo (pri 9) le robaba 226 contratos a Licencias (pri 10): Office, Adobe, MS Project, BI'),
    ('Desarrollo software', 'suscripcion a licencia',
     'fase4b: software es substring de "licencia de software"; sin esto, Desarrollo (pri 9) le robaba 226 contratos a Licencias (pri 10): Office, Adobe, MS Project, BI')
  ) AS v(categoria, keyword, nota)
  JOIN LATERAL (
    SELECT prioridad FROM it_keywords k
    WHERE k.categoria = v.categoria
    LIMIT 1
  ) p ON true
  ON CONFLICT (categoria, keyword, tipo) DO NOTHING;

  GET DIAGNOSTICS n_ex = ROW_COUNT;

  -- 1.2 Telemetria/OT 3 -> 11 (antes de Redes, despues de Licencias).
  -- Pri 3 hacia que SCADA secundario ganara sobre el objeto (88083 videovigilancia).
  -- Parqueo en 99, bajo 4..11 a 3..10, coloco 11. Sin UNIQUE(prioridad).
  UPDATE it_keywords
     SET prioridad = 99
   WHERE categoria = 'Telemetria/OT';

  UPDATE it_keywords
     SET prioridad = prioridad - 1
   WHERE prioridad BETWEEN 4 AND 11;

  UPDATE it_keywords
     SET prioridad = 11
   WHERE categoria = 'Telemetria/OT';

  SELECT count(*)::int INTO n_dup
  FROM (
    SELECT prioridad FROM it_keywords
    GROUP BY prioridad
    HAVING count(DISTINCT categoria) > 1
  ) d;

  SELECT count(*)::int INTO n_gap
  FROM generate_series(1, (SELECT max(prioridad) FROM it_keywords)) g(n)
  WHERE NOT EXISTS (SELECT 1 FROM it_keywords k WHERE k.prioridad = g.n);

  IF n_dup <> 0 OR n_gap <> 0 THEN
    RAISE EXCEPTION 'fase4b: prioridad invalida dup=% gap=%', n_dup, n_gap;
  END IF;

  INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
  VALUES (
    'c2_fase4b_correcciones',
    n_ex,
    jsonb_build_object(
      'excludes_software', n_ex,
      'telemetria_prioridad', 11,
      'nota_88083', 'videovigilancia con replicacion SCADA queda Redes'
    )
  );

  RAISE NOTICE 'C2 fase 4b: excludes=% dup=% gap=%', n_ex, n_dup, n_gap;
END $$;

-- 2.3 Mapeo de banda en el mismo archivo (prod + fuente en capa_semantica.sql).
CREATE OR REPLACE FUNCTION seace_rubro_linea(p_categoria_it text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT CASE p_categoria_it
    WHEN 'IA/analytics' THEN 'nucleo'
    WHEN 'Cloud/hosting' THEN 'nucleo'
    WHEN 'Desarrollo software' THEN 'nucleo'
    WHEN 'Telemetria/OT' THEN 'nucleo'
    WHEN 'Base de datos/ERP' THEN 'adyacente'
    WHEN 'Oracle' THEN 'adyacente'
    WHEN 'Soporte tecnico' THEN 'oportunista'
    WHEN 'Redes/cableado' THEN 'oportunista'
    WHEN 'Licencias' THEN 'oportunista'
    WHEN 'Ciberseguridad' THEN 'oportunista'
    WHEN 'Microsoft' THEN 'oportunista'
    WHEN 'Correo electronico' THEN 'oportunista'
    WHEN 'Firma digital' THEN 'oportunista'
    WHEN 'Hardware' THEN 'marginal'
    ELSE NULL
  END;
$$;

COMMENT ON COLUMN it_keywords.prioridad IS
  '1..14. Primera categoria por prioridad que gana. 11 = Telemetria/OT (NUCLEO; despues de Licencias, antes de Redes).';

COMMIT;
