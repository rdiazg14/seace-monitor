-- =====================================================================
-- chunk_version  ·  estado de chunking por contrato (migración 300/60)
-- =====================================================================
-- Permite marcar qué contratos ya se re-chunkearon a target=300 overlap=60
-- (chunking v2) y cuáles siguen en el esquema legacy 500/0.
--
--   '500_0'  (default) : chunking legacy (TARGET_SUBCHUNK=500, sin overlap)
--   '300_60'           : chunking v2 (target=300, overlap=60) + embebido
--
-- La migración se hace SOLO en postulables TI/IA (migrar_chunk_300_60.py)
-- para no gastar créditos de Gemini en todo el corpus. El resto queda en
-- '500_0' y se completa cuando haya créditos (el script es idempotente:
-- solo procesa chunk_version IS DISTINCT FROM '300_60').
-- =====================================================================

ALTER TABLE contratos
  ADD COLUMN IF NOT EXISTS chunk_version TEXT NOT NULL DEFAULT '500_0';

COMMENT ON COLUMN contratos.chunk_version IS
  'Version de chunking del TDR: 500_0 (legacy) | 300_60 (v2 target=300 overlap=60, embebido). Default 500_0.';

CREATE INDEX IF NOT EXISTS idx_contratos_chunk_version
  ON contratos (chunk_version);
