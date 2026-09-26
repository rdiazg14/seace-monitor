-- Aditivo. Trazabilidad del PDF del requerimiento (idContratoArchivo + nombre).
-- No toca embedding(768), buscar_tdr ni ivfflat.

ALTER TABLE contratos ADD COLUMN IF NOT EXISTS pdf_archivo_id BIGINT;
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS pdf_nombre     TEXT;

COMMENT ON COLUMN contratos.pdf_archivo_id IS 'idContratoArchivo del anexo PDF en SEACE.';
COMMENT ON COLUMN contratos.pdf_nombre     IS 'nombre original del archivo TDR/requerimiento.';
