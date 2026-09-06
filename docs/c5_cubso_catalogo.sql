-- C5: catalogo CUBSO oficial (OECE) + fila de version para el aviso anual.
-- IDEMPOTENTE: CREATE IF NOT EXISTS / DROP POLICY IF EXISTS.
-- migraciones_datos.nombre = c5_cubso_catalogo (registro; el schema se re-aplica).
-- La carga de filas la hace scripts/cargar_cubso.py, no este SQL.
--
-- El catalogo se usa como vocabulario tecnico normalizado, no como clasificador:
-- de 210 contratos etiquetados con CUBSO, 37 caen en segmento 43 y 86 en 81.

BEGIN;

CREATE TABLE IF NOT EXISTS migraciones_datos (
  nombre          text PRIMARY KEY,
  aplicada_utc    timestamptz NOT NULL DEFAULT now(),
  filas_afectadas int,
  detalle         jsonb
);

CREATE TABLE IF NOT EXISTS cubso_catalogo (
  codigo           char(16) PRIMARY KEY,
  titulo           text NOT NULL,
  tipo             text NOT NULL
                     CHECK (tipo IN ('BIENES', 'SERVICIOS', 'OBRAS', 'CONSULTORIAS OBRAS')),
  segmento         char(2)  NOT NULL,
  familia          char(4)  NOT NULL,
  clase            char(6)  NOT NULL,
  commodity        char(8)  NOT NULL,
  version_catalogo date NOT NULL,
  cargado_utc      timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE cubso_catalogo IS
  'Catalogo Unico de Bienes, Servicios y Obras (CUBSO) del OECE. codigo 16 digitos: 8 UNSPSC + 8 item peruano. No clasifica contratos; es vocabulario.';

COMMENT ON COLUMN cubso_catalogo.codigo IS
  '16 digitos. Prefijos: segmento 2, familia 4, clase 6, commodity 8 (UNSPSC).';

COMMENT ON COLUMN cubso_catalogo.tipo IS
  'BIENES | SERVICIOS | OBRAS | CONSULTORIAS OBRAS. Normalizado desde 1-BIENES, etc.';

COMMENT ON COLUMN cubso_catalogo.version_catalogo IS
  'Fecha de publicacion del archivo XLS cargado (scripts/cargar_cubso.py --version).';

CREATE TABLE IF NOT EXISTS cubso_version (
  id               int PRIMARY KEY DEFAULT 1,
  version_catalogo date NOT NULL,
  fuente_url       text,
  items            int,
  cargado_utc      timestamptz NOT NULL DEFAULT now(),
  CHECK (id = 1)
);

COMMENT ON TABLE cubso_version IS
  'Una sola fila (id=1). La lee /observabilidad para avisar si pasaron 12 meses.';

CREATE INDEX IF NOT EXISTS cubso_catalogo_segmento_idx
  ON cubso_catalogo (segmento);
CREATE INDEX IF NOT EXISTS cubso_catalogo_familia_idx
  ON cubso_catalogo (familia);
CREATE INDEX IF NOT EXISTS cubso_catalogo_commodity_idx
  ON cubso_catalogo (commodity);

-- pg_trgm: GIN sobre titulo si la extension esta (Supabase la suele tener).
DO $$
DECLARE
  tiene_trgm boolean;
BEGIN
  SELECT EXISTS (
    SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'
  ) INTO tiene_trgm;

  IF NOT tiene_trgm THEN
    BEGIN
      CREATE EXTENSION pg_trgm;
      tiene_trgm := true;
    EXCEPTION WHEN OTHERS THEN
      BEGIN
        CREATE EXTENSION pg_trgm WITH SCHEMA extensions;
        tiene_trgm := true;
      EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'C5: pg_trgm no disponible (%). Indice btree en titulo.', SQLERRM;
        tiene_trgm := false;
      END;
    END;
  END IF;

  IF tiene_trgm THEN
    EXECUTE
      'CREATE INDEX IF NOT EXISTS cubso_catalogo_titulo_trgm_idx '
      'ON cubso_catalogo USING gin (titulo gin_trgm_ops)';
    RAISE NOTICE 'C5: indice GIN pg_trgm en cubso_catalogo.titulo';
  ELSE
    EXECUTE
      'CREATE INDEX IF NOT EXISTS cubso_catalogo_titulo_idx '
      'ON cubso_catalogo (titulo)';
  END IF;
END $$;

ALTER TABLE cubso_catalogo ENABLE ROW LEVEL SECURITY;
ALTER TABLE cubso_version  ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON TABLE public.cubso_catalogo TO authenticated;
GRANT SELECT ON TABLE public.cubso_version  TO authenticated;

DROP POLICY IF EXISTS cubso_catalogo_select_admin ON public.cubso_catalogo;
CREATE POLICY cubso_catalogo_select_admin
  ON public.cubso_catalogo
  FOR SELECT
  TO authenticated
  USING (public.es_admin());

DROP POLICY IF EXISTS cubso_version_select_admin ON public.cubso_version;
CREATE POLICY cubso_version_select_admin
  ON public.cubso_version
  FOR SELECT
  TO authenticated
  USING (public.es_admin());

COMMENT ON POLICY cubso_catalogo_select_admin ON public.cubso_catalogo IS
  'Solo perfiles.rol = admin (es_admin). Escritura: service_role / pipeline.';

COMMENT ON POLICY cubso_version_select_admin ON public.cubso_version IS
  'Solo perfiles.rol = admin (es_admin). Escritura: service_role / pipeline.';

INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
VALUES (
  'c5_cubso_catalogo',
  0,
  '{"tablas": ["cubso_catalogo", "cubso_version"], "carga": "scripts/cargar_cubso.py"}'::jsonb
)
ON CONFLICT (nombre) DO NOTHING;

NOTIFY pgrst, 'reload schema';

COMMIT;
