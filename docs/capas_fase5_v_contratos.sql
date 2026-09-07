-- Capas fase 5: vista de compatibilidad v_contratos + lectores SQL.
-- EXACTAMENTE las mismas columnas que public.contratos (ordinal prod 2026-09-07).
-- categoria_it / relevancia_ia vienen de clasificacion_contrato (capa 3).
-- Front y Worker NO migran aqui: siguen en contratos via eco (fase 4).

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
  c.pdf_storage_bytes
FROM public.contratos c
LEFT JOIN public.clasificacion_contrato cl ON cl.contrato_id = c.id;

COMMENT ON VIEW public.v_contratos IS
  'Fase 5: mismos campos que contratos; categoria_it/relevancia_ia desde capa 3.';

GRANT SELECT ON public.v_contratos TO anon, authenticated;

NOTIFY pgrst, 'reload schema';
