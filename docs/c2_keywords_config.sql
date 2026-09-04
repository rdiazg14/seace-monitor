-- C2 fase 1: tabla de configuracion de keywords IT.
-- Replica IT_CATS (ingesta_completa.py) byte a byte. NO cambia el consumo:
-- la ingesta sigue leyendo la lista en codigo hasta C2 fase 2.
-- IDEMPOTENTE: si ya se aplico, no inserta de nuevo.

BEGIN;

CREATE TABLE IF NOT EXISTS migraciones_datos (
  nombre          text PRIMARY KEY,
  aplicada_utc    timestamptz NOT NULL DEFAULT now(),
  filas_afectadas int,
  detalle         jsonb
);

CREATE TABLE IF NOT EXISTS it_keywords (
  id             bigserial PRIMARY KEY,
  categoria      text NOT NULL,
  keyword        text NOT NULL,
  prioridad      int  NOT NULL,
  tipo           text NOT NULL DEFAULT 'incluye'
                   CHECK (tipo IN ('incluye', 'excluye')),
  limite_palabra boolean NOT NULL DEFAULT false,
  activa         boolean NOT NULL DEFAULT true,
  nota           text,
  creada_utc     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (categoria, keyword, tipo)
);

CREATE INDEX IF NOT EXISTS it_keywords_prioridad_categoria_idx
  ON it_keywords (prioridad, categoria);
CREATE INDEX IF NOT EXISTS it_keywords_activa_idx
  ON it_keywords (activa);

COMMENT ON TABLE it_keywords IS
  'Configuracion de keywords IT (C2). Carga inicial = IT_CATS. La ingesta no la consume hasta fase 2.';

COMMENT ON COLUMN it_keywords.keyword IS
  'Ya normalizada: minusculas, sin tildes. Espacios de borde (aws, monitor, ups, switch, wifi, sap, erp, tablet) son literales.';

COMMENT ON COLUMN it_keywords.prioridad IS
  '1..13, mismo orden que IT_CATS. Primera categoria por prioridad que gana.';

COMMENT ON COLUMN it_keywords.tipo IS
  'incluye: si matchea, la categoria puede ganar. excluye: si matchea el texto, esa categoria NO puede ganar aunque una incluye suya tambien matchee; la cascada sigue con la siguiente categoria por prioridad.';

COMMENT ON COLUMN it_keywords.limite_palabra IS
  'true = match con limite de palabra (\\b...\\b); false = substring crudo.';

ALTER TABLE it_keywords ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON TABLE public.it_keywords TO authenticated;

DROP POLICY IF EXISTS it_keywords_select_admin ON public.it_keywords;
CREATE POLICY it_keywords_select_admin
  ON public.it_keywords
  FOR SELECT
  TO authenticated
  USING (public.es_admin());

COMMENT ON POLICY it_keywords_select_admin ON public.it_keywords IS
  'Solo perfiles.rol = admin (es_admin). Escritura sigue siendo service_role / pipeline.';

DO $$
DECLARE
  n int;
BEGIN
  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'c2_keywords_config'
  ) THEN
    RAISE NOTICE 'C2 keywords ya aplicada (migraciones_datos.nombre=c2_keywords_config). No-op.';
    RETURN;
  END IF;

  INSERT INTO it_keywords (categoria, keyword, prioridad, tipo, limite_palabra, activa)
  VALUES
    ('Firma digital', 'firma digital', 1, 'incluye', false, true),
    ('Firma digital', 'certificado digital', 1, 'incluye', false, true),
    ('Firma digital', 'certificado electronico', 1, 'incluye', false, true),
    ('Firma digital', 'token criptografico', 1, 'incluye', false, true),
    ('IA/analytics', 'inteligencia artificial', 2, 'incluye', false, true),
    ('IA/analytics', 'machine learning', 2, 'incluye', false, true),
    ('IA/analytics', 'ia generativa', 2, 'incluye', false, true),
    ('IA/analytics', 'chatbot', 2, 'incluye', false, true),
    ('IA/analytics', 'asistente virtual', 2, 'incluye', false, true),
    ('IA/analytics', 'llm', 2, 'incluye', false, true),
    ('IA/analytics', 'gpt', 2, 'incluye', false, true),
    ('IA/analytics', 'copilot', 2, 'incluye', false, true),
    ('IA/analytics', 'gemini', 2, 'incluye', false, true),
    ('IA/analytics', 'claude', 2, 'incluye', false, true),
    ('IA/analytics', 'openai', 2, 'incluye', false, true),
    ('IA/analytics', 'azure openai', 2, 'incluye', false, true),
    ('IA/analytics', 'analytics', 2, 'incluye', false, true),
    ('IA/analytics', 'business intelligence', 2, 'incluye', false, true),
    ('IA/analytics', 'ciencia de datos', 2, 'incluye', false, true),
    ('IA/analytics', 'big data', 2, 'incluye', false, true),
    ('IA/analytics', 'procesamiento de lenguaje', 2, 'incluye', false, true),
    ('IA/analytics', 'red neuronal', 2, 'incluye', false, true),
    ('IA/analytics', 'deep learning', 2, 'incluye', false, true),
    ('IA/analytics', 'tokens de procesamiento', 2, 'incluye', false, true),
    ('Ciberseguridad', 'ciberseguridad', 3, 'incluye', false, true),
    ('Ciberseguridad', 'seguridad informatica', 3, 'incluye', false, true),
    ('Ciberseguridad', 'seguridad de la informacion', 3, 'incluye', false, true),
    ('Ciberseguridad', 'firewall', 3, 'incluye', false, true),
    ('Ciberseguridad', 'pentest', 3, 'incluye', false, true),
    ('Ciberseguridad', 'ethical hacking', 3, 'incluye', false, true),
    ('Cloud/hosting', 'nube publica', 4, 'incluye', false, true),
    ('Cloud/hosting', 'cloud computing', 4, 'incluye', false, true),
    ('Cloud/hosting', 'hosting', 4, 'incluye', false, true),
    ('Cloud/hosting', 'servidor virtual', 4, 'incluye', false, true),
    ('Cloud/hosting', ' aws ', 4, 'incluye', false, true),
    ('Cloud/hosting', 'google cloud', 4, 'incluye', false, true),
    ('Microsoft', 'microsoft', 5, 'incluye', false, true),
    ('Microsoft', 'office 365', 5, 'incluye', false, true),
    ('Microsoft', 'microsoft 365', 5, 'incluye', false, true),
    ('Microsoft', 'sharepoint', 5, 'incluye', false, true),
    ('Microsoft', 'exchange', 5, 'incluye', false, true),
    ('Microsoft', 'windows server', 5, 'incluye', false, true),
    ('Oracle', 'oracle database', 6, 'incluye', false, true),
    ('Oracle', 'oracle ebs', 6, 'incluye', false, true),
    ('Oracle', 'peoplesoft', 6, 'incluye', false, true),
    ('Base de datos/ERP', 'base de datos', 7, 'incluye', false, true),
    ('Base de datos/ERP', 'sql server', 7, 'incluye', false, true),
    ('Base de datos/ERP', 'postgresql', 7, 'incluye', false, true),
    ('Base de datos/ERP', 'mysql', 7, 'incluye', false, true),
    ('Base de datos/ERP', 'mongodb', 7, 'incluye', false, true),
    ('Base de datos/ERP', 'data warehouse', 7, 'incluye', false, true),
    ('Base de datos/ERP', ' sap ', 7, 'incluye', false, true),
    ('Base de datos/ERP', ' erp ', 7, 'incluye', false, true),
    ('Desarrollo software', 'desarrollo de software', 8, 'incluye', false, true),
    ('Desarrollo software', 'desarrollo de sistema', 8, 'incluye', false, true),
    ('Desarrollo software', 'sistema de informacion', 8, 'incluye', false, true),
    ('Desarrollo software', 'aplicativo', 8, 'incluye', false, true),
    ('Desarrollo software', 'software a medida', 8, 'incluye', false, true),
    ('Desarrollo software', 'plataforma web', 8, 'incluye', false, true),
    ('Desarrollo software', 'portal web', 8, 'incluye', false, true),
    ('Desarrollo software', 'sistema web', 8, 'incluye', false, true),
    ('Desarrollo software', 'sistema administrativo', 8, 'incluye', false, true),
    ('Desarrollo software', 'aplicacion movil', 8, 'incluye', false, true),
    ('Desarrollo software', 'app movil', 8, 'incluye', false, true),
    ('Desarrollo software', 'implementacion de software', 8, 'incluye', false, true),
    ('Licencias', 'licencia de software', 9, 'incluye', false, true),
    ('Licencias', 'licenciamiento', 9, 'incluye', false, true),
    ('Licencias', 'suscripcion de software', 9, 'incluye', false, true),
    ('Soporte tecnico', 'soporte tecnico', 10, 'incluye', false, true),
    ('Soporte tecnico', 'mantenimiento de software', 10, 'incluye', false, true),
    ('Soporte tecnico', 'mantenimiento de sistema', 10, 'incluye', false, true),
    ('Soporte tecnico', 'mesa de ayuda', 10, 'incluye', false, true),
    ('Soporte tecnico', 'helpdesk', 10, 'incluye', false, true),
    ('Soporte tecnico', 'help desk', 10, 'incluye', false, true),
    ('Redes/cableado', 'red de datos', 11, 'incluye', false, true),
    ('Redes/cableado', 'cableado estructurado', 11, 'incluye', false, true),
    ('Redes/cableado', ' switch ', 11, 'incluye', false, true),
    ('Redes/cableado', 'router', 11, 'incluye', false, true),
    ('Redes/cableado', 'fibra optica', 11, 'incluye', false, true),
    ('Redes/cableado', ' wifi', 11, 'incluye', false, true),
    ('Redes/cableado', 'wireless', 11, 'incluye', false, true),
    ('Redes/cableado', 'access point', 11, 'incluye', false, true),
    ('Redes/cableado', 'punto de acceso', 11, 'incluye', false, true),
    ('Correo electronico', 'correo electronico', 12, 'incluye', false, true),
    ('Correo electronico', 'mensajeria electronica', 12, 'incluye', false, true),
    ('Hardware', 'computadora', 13, 'incluye', false, true),
    ('Hardware', 'laptop', 13, 'incluye', false, true),
    ('Hardware', 'impresora', 13, 'incluye', false, true),
    ('Hardware', ' monitor ', 13, 'incluye', false, true),
    ('Hardware', 'disco duro', 13, 'incluye', false, true),
    ('Hardware', 'memoria ram', 13, 'incluye', false, true),
    ('Hardware', ' ups ', 13, 'incluye', false, true),
    ('Hardware', 'proyector', 13, 'incluye', false, true),
    ('Hardware', ' tablet ', 13, 'incluye', false, true),
    ('Hardware', 'equipos informaticos', 13, 'incluye', false, true),
    ('Hardware', 'equipos de computo', 13, 'incluye', false, true),
    ('Hardware', 'scanner', 13, 'incluye', false, true),
    ('Hardware', 'escaner', 13, 'incluye', false, true)
  ON CONFLICT (categoria, keyword, tipo) DO NOTHING;

  GET DIAGNOSTICS n = ROW_COUNT;

  INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
  VALUES (
    'c2_keywords_config',
    n,
    '{"categorias": 13, "keywords": 98, "tipo": "incluye", "fuente": "IT_CATS ingesta_completa.py"}'::jsonb
  );

  RAISE NOTICE 'C2 keywords aplicada: filas_insertadas=%', n;
END $$;

COMMIT;
