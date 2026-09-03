-- B21: SEACE entrega hora de pared de Lima; la ingesta la grababa como UTC.
-- Correccion: instante guardado + 5 h. Peru no tiene DST (offset fijo -05:00).
-- IDEMPOTENTE: si ya se aplico, no hace nada.
--
-- Corte hardcodeado = max(id) al preflight 2026-09-03 (id=91374).
-- Segunda defensa: filas nuevas (id > corte, ingesta ya con -05:00) no se tocan
-- aunque alguien borre el marcador y re-ejecute.
--
-- NO correr si el pipeline de ingesta esta activo.
-- Tras este script: verificar los SELECT del final, recien entonces deploy
-- de parsear_fecha con offset -05:00, recien entonces reanudar el cron.

BEGIN;

CREATE TABLE IF NOT EXISTS migraciones_datos (
  nombre          text PRIMARY KEY,
  aplicada_utc    timestamptz NOT NULL DEFAULT now(),
  filas_afectadas int,
  detalle         jsonb
);

DO $$
DECLARE
  n int;
BEGIN
  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'b21_timezone_lima'
  ) THEN
    RAISE NOTICE 'B21 ya aplicada (migraciones_datos.nombre=b21_timezone_lima). No-op.';
    RETURN;
  END IF;

  -- El literal 91374 define el ALCANCE del UPDATE; esta guardia detecta
  -- que el literal quedo corto. Sin ella, una fila que entre entre el
  -- preflight y la ejecucion queda sin corregir de forma indetectable.
  IF (SELECT max(id) FROM contratos) > 91374 THEN
    RAISE EXCEPTION 'B21: max(id)=% supera el corte 91374 del preflight. Hay filas nuevas sin corregir. Re-hacer el preflight y regenerar el script.',
      (SELECT max(id) FROM contratos);
  END IF;

  UPDATE contratos SET
    fecha_publica        = fecha_publica        + interval '5 hours',
    fecha_ini_cotizacion = fecha_ini_cotizacion + interval '5 hours',
    fecha_fin_cotizacion = fecha_fin_cotizacion + interval '5 hours'
  WHERE id <= 91374;

  GET DIAGNOSTICS n = ROW_COUNT;

  INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
  VALUES (
    'b21_timezone_lima',
    n,
    '{"id_corte": 91374, "columnas": ["fecha_publica", "fecha_ini_cotizacion", "fecha_fin_cotizacion"], "shift": "+5h"}'::jsonb
  );

  RAISE NOTICE 'B21 aplicada: filas_afectadas=%', n;
END $$;

COMMIT;

-- ---------------------------------------------------------------------------
-- correr despues (descomentar y ejecutar aparte):
-- ---------------------------------------------------------------------------
--
-- 1) Distribucion por hora Lima de fecha_fin_cotizacion, ultimos 60 dias.
--    Despues de UN apply, debe parecerse a la hora GRABADA pre-fix (~78% en
--    08-17). Antes del apply este mismo SELECT da ~49% (UTC-5 del instante
--    mal etiquetado). Si tras el apply el % se aleja de ~78% (pico corrido
--    a 13-22 o vuelta a madrugada), el +5h se aplico dos veces.
--
-- SELECT
--   extract(hour FROM timezone('America/Lima', fecha_fin_cotizacion))::int AS hora_lima,
--   count(*) AS n
-- FROM contratos
-- WHERE fecha_fin_cotizacion >= now() - interval '60 days'
-- GROUP BY 1
-- ORDER BY 1;
--
-- 2) % en horario habil 08:00-17:00 Lima (esperable ~78% tras un apply).
--
-- SELECT
--   round(100.0 * count(*) FILTER (
--     WHERE extract(hour FROM timezone('America/Lima', fecha_fin_cotizacion))
--           BETWEEN 8 AND 17
--   ) / nullif(count(*), 0), 1) AS pct_habil_8_17,
--   count(*) AS n
-- FROM contratos
-- WHERE fecha_fin_cotizacion >= now() - interval '60 days';
--
-- 3) Marcador.
--
-- SELECT * FROM migraciones_datos WHERE nombre = 'b21_timezone_lima';
