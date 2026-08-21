-- Expand: marcas permanentes de funnel #10/#11 en contratos.
-- No toca columnas existentes, RLS, grants ni vistas v_kpis_*.
-- No hay monto referencial (SEACE no lo expone en las APIs que usamos).
--
-- Las filas actuales (~76k) quedan analizado=FALSE / cotizado=FALSE
-- sin backfill: no reconstruimos historia pre-marca.
-- fecha_* queda NULL hasta la primera marca (reconciliación posterior).
--
-- Semántica (la aplicará el job de reconciliación, no este SQL):
--   analizado/cotizado son ACUMULATIVOS y PERMANENTES (TRUE no vuelve a FALSE).
--   fecha_analisis / fecha_cotizacion se setean una sola vez (primera).

ALTER TABLE contratos ADD COLUMN IF NOT EXISTS analizado BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS cotizado BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS fecha_analisis TIMESTAMPTZ;
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS fecha_cotizacion TIMESTAMPTZ;

COMMENT ON COLUMN contratos.analizado IS
  'Funnel #10. TRUE permanente desde el primer análisis. DEFAULT FALSE = nunca marcado (no implica que no se haya analizado antes de esta columna).';
COMMENT ON COLUMN contratos.cotizado IS
  'Funnel #11. TRUE permanente desde la primera cotización. DEFAULT FALSE = nunca marcado.';
COMMENT ON COLUMN contratos.fecha_analisis IS
  'Primera vez que analizado pasó a TRUE. NULL hasta esa marca.';
COMMENT ON COLUMN contratos.fecha_cotizacion IS
  'Primera vez que cotizado pasó a TRUE. NULL hasta esa marca.';

-- Tras aplicar en el SQL Editor de Supabase, recargar cache de PostgREST:
--   NOTIFY pgrst, 'reload schema';
