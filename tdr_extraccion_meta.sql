-- Aditivo. Trazabilidad de extracción TDR por página (nativo / mixto / imagen).
-- No toca embedding(768), buscar_tdr ni ivfflat.

ALTER TABLE contratos ADD COLUMN IF NOT EXISTS tdr_tipo_extraccion TEXT;
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS paginas_ocr_pendientes JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS paginas_ocr_hechas JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS tdr_n_paginas INT;
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS tdr_n_paginas_nativas INT;
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS tdr_n_paginas_ocr INT;

ALTER TABLE contratos DROP CONSTRAINT IF EXISTS contratos_tdr_tipo_extraccion_chk;
ALTER TABLE contratos ADD CONSTRAINT contratos_tdr_tipo_extraccion_chk
  CHECK (
    tdr_tipo_extraccion IS NULL
    OR tdr_tipo_extraccion IN ('nativo_puro', 'mixto', 'imagen_total')
  );

COMMENT ON COLUMN contratos.tdr_tipo_extraccion IS
  'nativo_puro | mixto | imagen_total. NULL si no hay PDF (sin_pdf).';
COMMENT ON COLUMN contratos.paginas_ocr_pendientes IS
  'Páginas 1-based que aún faltan OCR. [] si no hay pendientes.';
COMMENT ON COLUMN contratos.paginas_ocr_hechas IS
  'Páginas 1-based ya OCR-eadas (trazabilidad).';
COMMENT ON COLUMN contratos.tdr_n_paginas IS 'Total de páginas del PDF.';
COMMENT ON COLUMN contratos.tdr_n_paginas_nativas IS 'Páginas con texto PyMuPDF (>= 80 chars).';
COMMENT ON COLUMN contratos.tdr_n_paginas_ocr IS 'Páginas imagen pendientes o ya OCR (conteo).';

-- Tras ALTER, recarga el cache de PostgREST (si no, --sync-meta no escribe mixto/imagen):
--   NOTIFY pgrst, 'reload schema';

-- Los nativos del PASO 1 (pdf_es_imagen no true, con tdr_texto, no sin_pdf).
-- mixto / imagen_total NO se infieren aquí: salen de data/tdr_extraccion.jsonl
-- vía `uv run python descargar_requerimiento.py --sync-meta --reporte`.
UPDATE contratos
SET tdr_tipo_extraccion = 'nativo_puro',
    pdf_es_imagen = FALSE,
    paginas_ocr_pendientes = '[]'::jsonb,
    paginas_ocr_hechas = '[]'::jsonb,
    tdr_n_paginas_ocr = 0
WHERE estado = 'Vigente'
  AND pdf_descargado IS TRUE
  AND COALESCE(req_url, '') <> 'sin_pdf'
  AND tdr_texto IS NOT NULL
  AND COALESCE(pdf_es_imagen, FALSE) IS FALSE
  AND tdr_tipo_extraccion IS NULL;

SELECT
  COUNT(*) FILTER (WHERE tdr_tipo_extraccion = 'nativo_puro') AS nativo_puro,
  COUNT(*) FILTER (WHERE tdr_tipo_extraccion = 'mixto') AS mixto,
  COUNT(*) FILTER (WHERE tdr_tipo_extraccion = 'imagen_total') AS imagen_total,
  COUNT(*) FILTER (WHERE req_url = 'sin_pdf') AS sin_pdf,
  COUNT(*) FILTER (WHERE req_url = 'pendiente_ocr') AS pendiente_ocr_viejo
FROM contratos
WHERE estado = 'Vigente';

-- Tras una corrida --solo-nativo sin estas columnas, aplica el sidecar:
--   uv run python descargar_requerimiento.py --sync-meta --reporte
