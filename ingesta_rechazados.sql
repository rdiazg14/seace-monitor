-- =====================================================================
-- G2 dead-letter  ·  SEACE Monitor  ·  ADITIVO e IDEMPOTENTE
-- =====================================================================
-- No toca contratos, chunks_tdr, embedding(768), buscar_tdr ni ivfflat.
-- Ejecutar en: Supabase → SQL Editor (seguro re-ejecutar).
-- =====================================================================

CREATE TABLE IF NOT EXISTS ingesta_rechazados (
  id           BIGSERIAL PRIMARY KEY,
  id_contrato  BIGINT,
  origen       TEXT,
  motivo       TEXT        NOT NULL,
  payload      JSONB       NOT NULL,
  resuelto     BOOLEAN     NOT NULL DEFAULT FALSE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Si la tabla ya existía sin estas columnas:
ALTER TABLE ingesta_rechazados ADD COLUMN IF NOT EXISTS origen   TEXT;
ALTER TABLE ingesta_rechazados ADD COLUMN IF NOT EXISTS resuelto BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON TABLE  ingesta_rechazados IS
  'G2: registros SEACE que no pasan el esquema. No entran a contratos.';
COMMENT ON COLUMN ingesta_rechazados.id_contrato IS
  'idContrato si se pudo leer; NULL si el payload no traía id válido.';
COMMENT ON COLUMN ingesta_rechazados.origen IS
  'Proceso que rechazó: ingesta | refresh | pdf.';
COMMENT ON COLUMN ingesta_rechazados.motivo IS
  'Mensaje de ValidationError (o excepción al mapear).';
COMMENT ON COLUMN ingesta_rechazados.payload IS
  'SOLO el registro de datos de la API. Nunca cookies, headers ni tokens.';
COMMENT ON COLUMN ingesta_rechazados.resuelto IS
  'TRUE cuando ya se revisó; no se borra la fila.';

CREATE INDEX IF NOT EXISTS idx_ingesta_rechazados_created
  ON ingesta_rechazados (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ingesta_rechazados_id_contrato
  ON ingesta_rechazados (id_contrato)
  WHERE id_contrato IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ingesta_rechazados_pendientes
  ON ingesta_rechazados (created_at DESC)
  WHERE resuelto = FALSE;

ALTER TABLE ingesta_rechazados ENABLE ROW LEVEL SECURITY;

SELECT
  EXISTS (SELECT 1 FROM pg_tables WHERE tablename = 'ingesta_rechazados') AS tabla_ok;
