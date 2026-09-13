-- ============================================================
-- Iteración «Ruta del día v2»
-- 1) etapas_json en contratos + vista v_contratos
-- 2) tabla ruta_ocultos (ocultar proyectos por usuario) + RLS
-- Ejecutar con: uv run python scripts/run_sql.py docs/mejora_ruta_dia_v2.sql
-- Idempotente (IF NOT EXISTS / CREATE OR REPLACE).
-- ============================================================

-- 1. Cronograma de etapas (consultas/absoluciones, cotización, …)
ALTER TABLE IF EXISTS public.contratos
  ADD COLUMN IF NOT EXISTS etapas_json jsonb;

-- 2. Vista v_contratos: misma proyección + etapas_json
CREATE OR REPLACE VIEW public.v_contratos AS
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
  cl.categoria_it,
  cl.relevancia_ia,
  c.texto_busqueda,
  c.created_at,
  c.nom_area_usuaria,
  c.items_json,
  c.detalle_cargado,
  c.req_url,
  c.pdf_descargado,
  c.pdf_procesado,
  c.pdf_es_imagen,
  c.tdr_texto,
  c.pdf_hash,
  c.estado_verificado_at,
  c.pdf_archivo_id,
  c.pdf_nombre,
  c.tdr_tipo_extraccion,
  c.paginas_ocr_pendientes,
  c.paginas_ocr_hechas,
  c.tdr_n_paginas,
  c.tdr_n_paginas_nativas,
  c.tdr_n_paginas_ocr,
  c.analizado,
  c.cotizado,
  c.fecha_analisis,
  c.fecha_cotizacion,
  c.pdf_storage_path,
  c.pdf_storage_at,
  c.pdf_storage_bytes,
  c.etapas_json
FROM public.contratos c
LEFT JOIN public.clasificacion_contrato cl ON cl.contrato_id = c.id;

ALTER VIEW public.v_contratos SET (security_invoker = true);

GRANT SELECT ON public.v_contratos TO anon, authenticated;

-- 3. Proyectos ocultos por usuario (clave compuesta user + contrato)
CREATE TABLE IF NOT EXISTS public.ruta_ocultos (
  user_id      uuid        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  contrato_id  bigint      NOT NULL REFERENCES public.contratos(id) ON DELETE CASCADE,
  ocultado_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, contrato_id)
);

CREATE INDEX IF NOT EXISTS idx_ruta_ocultos_user
  ON public.ruta_ocultos(user_id);

ALTER TABLE public.ruta_ocultos ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "ocultos_lectura_propia" ON public.ruta_ocultos;
CREATE POLICY "ocultos_lectura_propia" ON public.ruta_ocultos
  FOR SELECT TO authenticated USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "ocultos_insercion_propia" ON public.ruta_ocultos;
CREATE POLICY "ocultos_insercion_propia" ON public.ruta_ocultos
  FOR INSERT TO authenticated WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS "ocultos_borrado_propio" ON public.ruta_ocultos;
CREATE POLICY "ocultos_borrado_propio" ON public.ruta_ocultos
  FOR DELETE TO authenticated USING (auth.uid() = user_id);

GRANT SELECT, INSERT, DELETE ON public.ruta_ocultos TO authenticated;

COMMENT ON TABLE public.ruta_ocultos IS
  'Contratos que el usuario ocultó en Ruta del día. Una fila por (user_id, contrato_id).';

NOTIFY pgrst, 'reload schema';
