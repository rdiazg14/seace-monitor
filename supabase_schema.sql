-- ============================================================
-- SEACE Monitor — Schema Supabase
-- Ejecutar completo en: SQL Editor → New query → Run All
-- ============================================================

-- 1. Tabla principal
CREATE TABLE IF NOT EXISTS contratos (
  id                    BIGINT       PRIMARY KEY,     -- idContrato del SEACE
  nro_contratacion      TEXT,                          -- nroContratacion (número puro)
  descripcion_contrato  TEXT,                          -- desContratacion  (código CM-...)
  objeto                TEXT,                          -- Bien / Servicio / Obra / Consultoría de Obra
  descripcion           TEXT,                          -- desObjetoContrato (texto largo)
  entidad               TEXT,                          -- nomEntidad
  estado                TEXT,                          -- Vigente / En Evaluación / Culminado
  fecha_publica         TIMESTAMPTZ,
  fecha_ini_cotizacion  TIMESTAMPTZ,
  fecha_fin_cotizacion  TIMESTAMPTZ,
  tipo_cotizacion       TEXT,
  cotizar               BOOLEAN,
  categoria_it          TEXT,                          -- NULL si no es IT
  relevancia_ia         TEXT,                          -- ALTA / MEDIA / BAJA / NULL
  texto_busqueda        TSVECTOR,                      -- generado por trigger
  created_at            TIMESTAMPTZ  DEFAULT NOW()
);

-- 2. Índices
CREATE INDEX IF NOT EXISTS idx_contratos_fts
  ON contratos USING GIN(texto_busqueda);

CREATE INDEX IF NOT EXISTS idx_contratos_estado
  ON contratos(estado);

CREATE INDEX IF NOT EXISTS idx_contratos_objeto
  ON contratos(objeto);

CREATE INDEX IF NOT EXISTS idx_contratos_entidad
  ON contratos(entidad);

CREATE INDEX IF NOT EXISTS idx_contratos_fecha
  ON contratos(fecha_publica DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_contratos_catit
  ON contratos(categoria_it)
  WHERE categoria_it IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_contratos_fecha_fin_vigente
  ON contratos(fecha_fin_cotizacion)
  WHERE estado = 'Vigente';

-- 3. Función para generar texto_busqueda automáticamente
CREATE OR REPLACE FUNCTION fn_update_texto_busqueda()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  NEW.texto_busqueda := to_tsvector(
    'spanish',
    coalesce(NEW.descripcion, '')           || ' ' ||
    coalesce(NEW.entidad, '')               || ' ' ||
    coalesce(NEW.descripcion_contrato, '')  || ' ' ||
    coalesce(NEW.objeto, '')
  );
  RETURN NEW;
END;
$$;

-- 4. Trigger (recrea si ya existe)
DROP TRIGGER IF EXISTS trg_texto_busqueda ON contratos;
CREATE TRIGGER trg_texto_busqueda
  BEFORE INSERT OR UPDATE ON contratos
  FOR EACH ROW EXECUTE FUNCTION fn_update_texto_busqueda();

-- 5. Función de búsqueda full-text con ranking y filtros
CREATE OR REPLACE FUNCTION buscar_contratos(
  termino         TEXT  DEFAULT '',
  filtro_objeto   TEXT  DEFAULT NULL,
  filtro_estado   TEXT  DEFAULT NULL,
  filtro_entidad  TEXT  DEFAULT NULL,
  limite          INT   DEFAULT 50,
  offset_val      INT   DEFAULT 0
)
RETURNS TABLE (
  id                    BIGINT,
  nro_contratacion      TEXT,
  descripcion_contrato  TEXT,
  objeto                TEXT,
  descripcion           TEXT,
  entidad               TEXT,
  estado                TEXT,
  fecha_publica         TIMESTAMPTZ,
  fecha_ini_cotizacion  TIMESTAMPTZ,
  fecha_fin_cotizacion  TIMESTAMPTZ,
  tipo_cotizacion       TEXT,
  cotizar               BOOLEAN,
  categoria_it          TEXT,
  relevancia_ia         TEXT,
  rank                  REAL
)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  tsq tsquery;
BEGIN
  BEGIN
    IF termino IS NOT NULL AND trim(termino) <> '' THEN
      tsq := plainto_tsquery('spanish', termino);
    END IF;
  EXCEPTION WHEN OTHERS THEN
    tsq := NULL;
  END;

  RETURN QUERY
  SELECT
    c.id,
    c.nro_contratacion,
    c.descripcion_contrato,
    c.objeto,
    c.descripcion,
    c.entidad,
    c.estado,
    c.fecha_publica,
    c.fecha_ini_cotizacion,
    c.fecha_fin_cotizacion,
    c.tipo_cotizacion,
    c.cotizar,
    c.categoria_it,
    c.relevancia_ia,
    CASE
      WHEN tsq IS NOT NULL THEN ts_rank(c.texto_busqueda, tsq)
      ELSE 1.0::REAL
    END AS rank
  FROM contratos c
  WHERE
    (tsq IS NULL OR c.texto_busqueda @@ tsq)
    AND (filtro_objeto  IS NULL OR c.objeto  = filtro_objeto)
    AND (filtro_estado  IS NULL OR c.estado  = filtro_estado)
    AND (filtro_entidad IS NULL OR c.entidad ILIKE '%' || filtro_entidad || '%')
  ORDER BY
    CASE WHEN tsq IS NOT NULL THEN ts_rank(c.texto_busqueda, tsq)
         ELSE 1.0::REAL END DESC,
    c.fecha_publica DESC NULLS LAST
  LIMIT  LEAST(limite, 200)
  OFFSET offset_val;
END;
$$;

-- 6. Permisos: anon y authenticated pueden ejecutar la función y leer la tabla
GRANT EXECUTE ON FUNCTION buscar_contratos(TEXT, TEXT, TEXT, TEXT, INT, INT)
  TO anon, authenticated;

GRANT SELECT ON contratos TO anon, authenticated;

-- 7. Vista: resumen para dashboard (contratos por mes, objeto, estado, categoría IT)
CREATE OR REPLACE VIEW dashboard_resumen AS
SELECT
  objeto,
  estado,
  categoria_it,
  DATE_TRUNC('month', fecha_publica)::DATE AS mes,
  COUNT(*)::INT                            AS total
FROM contratos
GROUP BY
  objeto,
  estado,
  categoria_it,
  DATE_TRUNC('month', fecha_publica)::DATE;

GRANT SELECT ON dashboard_resumen TO anon, authenticated;

-- 8. Vista: contratos vigentes ordenados por urgencia de cierre
CREATE OR REPLACE VIEW vigentes_urgentes AS
SELECT *
FROM contratos
WHERE estado = 'Vigente'
ORDER BY fecha_fin_cotizacion ASC NULLS LAST;

GRANT SELECT ON vigentes_urgentes TO anon, authenticated;

-- 9. RLS: habilitar y política de lectura pública
--    (datos del SEACE = información pública del Estado peruano)
ALTER TABLE contratos ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Lectura publica" ON contratos;
CREATE POLICY "Lectura publica" ON contratos
  FOR SELECT TO anon, authenticated
  USING (true);

-- Verificación final
SELECT
  'Schema OK' AS resultado,
  (SELECT COUNT(*) FROM information_schema.tables
   WHERE table_name = 'contratos' AND table_schema = 'public') AS tabla_existe,
  (SELECT COUNT(*) FROM information_schema.routines
   WHERE routine_name = 'buscar_contratos' AND routine_schema = 'public') AS funcion_existe;
