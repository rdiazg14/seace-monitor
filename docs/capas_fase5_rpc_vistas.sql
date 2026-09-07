-- Capas fase 5: aplicar lectores SQL (despues de v_contratos).
-- Orden: 1) docs/capas_fase5_v_contratos.sql  2) este archivo
-- Incluye: capa_semantica vistas (ya en capa_semantica.sql), conversion,
-- buscar_contratos / dashboard_resumen / vigentes_urgentes.

-- Re-aplicar capa semantica completa es seguro (OR REPLACE).
-- Aqui solo los objetos de supabase_schema que migran a v_contratos.

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
  FROM v_contratos c
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

CREATE OR REPLACE VIEW dashboard_resumen AS
SELECT
  objeto,
  estado,
  categoria_it,
  DATE_TRUNC('month', fecha_publica)::DATE AS mes,
  COUNT(*)::INT                            AS total
FROM v_contratos
GROUP BY
  objeto,
  estado,
  categoria_it,
  DATE_TRUNC('month', fecha_publica)::DATE;

CREATE OR REPLACE VIEW vigentes_urgentes AS
SELECT *
FROM v_contratos
WHERE estado = 'Vigente'
ORDER BY fecha_fin_cotizacion ASC NULLS LAST;

GRANT EXECUTE ON FUNCTION buscar_contratos(TEXT, TEXT, TEXT, TEXT, INT, INT)
  TO anon, authenticated;
GRANT SELECT ON dashboard_resumen TO anon, authenticated;
GRANT SELECT ON vigentes_urgentes TO anon, authenticated;

NOTIFY pgrst, 'reload schema';
