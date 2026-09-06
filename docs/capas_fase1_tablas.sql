-- Capas fase 1: tablas nuevas VACIAS (expand). Sin backfill. Sin dual-write.
-- contrato_items, documentos, clasificacion_contrato, keyword_candidatas,
-- analisis_contrato. Indices + RLS. NO toca columnas de contratos.
-- IDEMPOTENTE: CREATE IF NOT EXISTS / DROP POLICY IF EXISTS.
-- marcador migraciones_datos.nombre = capas_fase1_tablas.
--
-- Ejecutar: uv run python scripts/run_sql.py docs/capas_fase1_tablas.sql
-- NO es fase 2: clasificacion_contrato queda en 0 filas.

BEGIN;

CREATE TABLE IF NOT EXISTS migraciones_datos (
  nombre          text PRIMARY KEY,
  aplicada_utc    timestamptz NOT NULL DEFAULT now(),
  filas_afectadas int,
  detalle         jsonb
);

-- ── Capa 1: contrato_items ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.contrato_items (
  contrato_id  bigint  NOT NULL REFERENCES public.contratos(id) ON DELETE CASCADE,
  item_nro     int     NOT NULL,
  cod_cubso    text,
  nom_cubso    text,
  descripcion  text,
  cantidad     numeric,
  unidad       text,
  distrito     text,
  PRIMARY KEY (contrato_id, item_nro)
);

COMMENT ON TABLE public.contrato_items IS
  'Capa 1. Items CUBSO declarados (antes items_json). Sin FK a cubso_catalogo.';

COMMENT ON COLUMN public.contrato_items.item_nro IS
  'Orden 1-based en uitContratoItemProjectionList.';

CREATE INDEX IF NOT EXISTS contrato_items_cod_cubso_idx
  ON public.contrato_items (cod_cubso)
  WHERE cod_cubso IS NOT NULL;

CREATE INDEX IF NOT EXISTS contrato_items_contrato_idx
  ON public.contrato_items (contrato_id);

ALTER TABLE public.contrato_items ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON TABLE public.contrato_items TO anon, authenticated;

DROP POLICY IF EXISTS contrato_items_select_public ON public.contrato_items;
CREATE POLICY contrato_items_select_public
  ON public.contrato_items
  FOR SELECT TO anon, authenticated
  USING (true);

-- ── Capa 1: documentos ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.documentos (
  id              bigserial PRIMARY KEY,
  contrato_id     bigint NOT NULL REFERENCES public.contratos(id) ON DELETE CASCADE,
  pdf_archivo_id  bigint,
  storage_path    text,
  sha256          text,
  bytes           bigint,
  paginas         int,
  tipo_extraccion text
                    CHECK (tipo_extraccion IS NULL
                           OR tipo_extraccion IN ('nativo', 'ocr', 'mixto')),
  descargado_utc  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (contrato_id, pdf_archivo_id)
);

COMMENT ON TABLE public.documentos IS
  'Capa 1. Data lake de anexos PDF. Un contrato puede tener mas de un anexo.';

CREATE INDEX IF NOT EXISTS documentos_contrato_idx
  ON public.documentos (contrato_id);

CREATE INDEX IF NOT EXISTS documentos_sha256_idx
  ON public.documentos (sha256)
  WHERE sha256 IS NOT NULL;

ALTER TABLE public.documentos ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON TABLE public.documentos TO anon, authenticated;

DROP POLICY IF EXISTS documentos_select_public ON public.documentos;
CREATE POLICY documentos_select_public
  ON public.documentos
  FOR SELECT TO anon, authenticated
  USING (true);

-- ── Capa 3: clasificacion_contrato ───────────────────────────────────

CREATE TABLE IF NOT EXISTS public.clasificacion_contrato (
  contrato_id      bigint PRIMARY KEY
                     REFERENCES public.contratos(id) ON DELETE CASCADE,
  categoria_it     text,
  relevancia_ia    text,
  capa             text NOT NULL
                     CHECK (capa IN ('keyword', 'gemini', 'cubso', 'humano')),
  keyword_id       bigint
                     REFERENCES public.it_keywords(id) ON DELETE SET NULL,
  senal            text,
  senal_fuente     text,
  confianza        numeric,
  consenso_n       int NOT NULL DEFAULT 0,
  revisar          boolean NOT NULL DEFAULT false,
  artefacto        text,
  creado_utc       timestamptz NOT NULL DEFAULT now(),
  actualizado_utc  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT clasificacion_tiene_etiqueta_chk
    CHECK (categoria_it IS NOT NULL OR relevancia_ia IS NOT NULL)
);

COMMENT ON TABLE public.clasificacion_contrato IS
  'Capa 3. Una clasificacion vigente por contrato. Escritura: pipeline. Nunca el browser ni la ingesta.';

CREATE INDEX IF NOT EXISTS clasificacion_categoria_idx
  ON public.clasificacion_contrato (categoria_it)
  WHERE categoria_it IS NOT NULL;

CREATE INDEX IF NOT EXISTS clasificacion_ia_idx
  ON public.clasificacion_contrato (relevancia_ia)
  WHERE relevancia_ia IS NOT NULL;

CREATE INDEX IF NOT EXISTS clasificacion_capa_idx
  ON public.clasificacion_contrato (capa);

CREATE INDEX IF NOT EXISTS clasificacion_revisar_idx
  ON public.clasificacion_contrato (contrato_id)
  WHERE revisar;

ALTER TABLE public.clasificacion_contrato ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON TABLE public.clasificacion_contrato TO anon, authenticated;

DROP POLICY IF EXISTS clasificacion_select_public ON public.clasificacion_contrato;
CREATE POLICY clasificacion_select_public
  ON public.clasificacion_contrato
  FOR SELECT TO anon, authenticated
  USING (true);

CREATE OR REPLACE FUNCTION public.fn_clasificacion_touch()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.actualizado_utc := now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_clasificacion_touch ON public.clasificacion_contrato;
CREATE TRIGGER trg_clasificacion_touch
  BEFORE UPDATE ON public.clasificacion_contrato
  FOR EACH ROW EXECUTE FUNCTION public.fn_clasificacion_touch();

-- ── Capa 3: keyword_candidatas ───────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.keyword_candidatas (
  id                    bigserial PRIMARY KEY,
  senal                 text NOT NULL,
  categoria_propuesta   text NOT NULL,
  veces_vista           int  NOT NULL DEFAULT 1,
  contratos             bigint[] NOT NULL DEFAULT '{}',
  ejemplo_contrato_id   bigint REFERENCES public.contratos(id) ON DELETE SET NULL,
  primera_vez_utc       timestamptz NOT NULL DEFAULT now(),
  ultima_vez_utc        timestamptz NOT NULL DEFAULT now(),
  estado                text NOT NULL DEFAULT 'nueva'
                          CHECK (estado IN (
                            'nueva', 'medida', 'auto_activada',
                            'aprobada_admin', 'rechazada'
                          )),
  tipo_eval             text
                          CHECK (tipo_eval IS NULL OR tipo_eval IN ('a', 'b')),
  keyword_madre_id      bigint REFERENCES public.it_keywords(id) ON DELETE SET NULL,
  keyword_id            bigint REFERENCES public.it_keywords(id) ON DELETE SET NULL,
  universo_a            int,
  cambios_categoria     int,
  ya_etiquetados        int,
  ratio_predictivo      numeric,
  evidencia             jsonb,
  evaluada_utc          timestamptz,
  activada_utc          timestamptz,
  activada_por          text,
  nota                  text,
  UNIQUE (senal, categoria_propuesta)
);

COMMENT ON TABLE public.keyword_candidatas IS
  'Capa 3. Senales que Gemini vio y it_keywords no. Tipo A/B se evaluan solas. Nunca auto-activar sobre UMBRAL_AUTO.';

CREATE INDEX IF NOT EXISTS keyword_candidatas_estado_idx
  ON public.keyword_candidatas (estado);

CREATE INDEX IF NOT EXISTS keyword_candidatas_contratos_gin
  ON public.keyword_candidatas USING GIN (contratos);

ALTER TABLE public.keyword_candidatas ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON TABLE public.keyword_candidatas TO authenticated;

DROP POLICY IF EXISTS keyword_candidatas_select_admin ON public.keyword_candidatas;
CREATE POLICY keyword_candidatas_select_admin
  ON public.keyword_candidatas
  FOR SELECT TO authenticated
  USING (public.es_admin());

-- ── Capa 4: analisis_contrato ────────────────────────────────────────

CREATE TABLE IF NOT EXISTS public.analisis_contrato (
  contrato_id     bigint NOT NULL REFERENCES public.contratos(id) ON DELETE CASCADE,
  pdf_hash        text   NOT NULL DEFAULT 'na',
  prompt_version  text   NOT NULL,
  modelo          text,
  tdr_fuente      text,
  tdr_chars       int,
  payload         jsonb  NOT NULL,
  creado_utc      timestamptz NOT NULL DEFAULT now(),
  actualizado_utc timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (contrato_id, pdf_hash)
);

COMMENT ON TABLE public.analisis_contrato IS
  'Capa 4. Cache durable de POST /analizar. payload = AnalisisPayload.';

CREATE INDEX IF NOT EXISTS analisis_contrato_creado_idx
  ON public.analisis_contrato (creado_utc DESC);

ALTER TABLE public.analisis_contrato ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON TABLE public.analisis_contrato TO anon, authenticated;

DROP POLICY IF EXISTS analisis_select_public ON public.analisis_contrato;
CREATE POLICY analisis_select_public
  ON public.analisis_contrato
  FOR SELECT TO anon, authenticated
  USING (true);

-- ── Marcador + conteos ───────────────────────────────────────────────

INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
VALUES (
  'capas_fase1_tablas',
  0,
  '{"tablas": ["contrato_items", "documentos", "clasificacion_contrato", "keyword_candidatas", "analisis_contrato"], "filas": 0}'::jsonb
)
ON CONFLICT (nombre) DO NOTHING;

DO $$
DECLARE
  n_items int;
  n_docs int;
  n_claf int;
  n_cand int;
  n_anal int;
  ya boolean;
BEGIN
  SELECT EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'capas_fase1_tablas'
  ) INTO ya;
  SELECT count(*) INTO n_items FROM public.contrato_items;
  SELECT count(*) INTO n_docs  FROM public.documentos;
  SELECT count(*) INTO n_claf  FROM public.clasificacion_contrato;
  SELECT count(*) INTO n_cand  FROM public.keyword_candidatas;
  SELECT count(*) INTO n_anal  FROM public.analisis_contrato;
  RAISE NOTICE 'capas fase 1: marcador=% contrato_items=% documentos=% clasificacion_contrato=% keyword_candidatas=% analisis_contrato=%',
    ya, n_items, n_docs, n_claf, n_cand, n_anal;
END $$;

NOTIFY pgrst, 'reload schema';

COMMIT;
