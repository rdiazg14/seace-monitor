-- Aditivo. Metadata de contrato en el chunk (filtro), no dentro del vector.
-- No toca embedding(768), buscar_tdr ni ivfflat.

ALTER TABLE chunks_tdr ADD COLUMN IF NOT EXISTS meta_entidad TEXT;
ALTER TABLE chunks_tdr ADD COLUMN IF NOT EXISTS meta_nro     TEXT;

COMMENT ON COLUMN chunks_tdr.meta_entidad IS 'Entidad del contrato (filtro). No se embebe.';
COMMENT ON COLUMN chunks_tdr.meta_nro     IS 'Nº de contratación (filtro). No se embebe.';
