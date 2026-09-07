-- Capas fase 4: trigger de eco clasificacion_contrato -> contratos.
-- IDEMPOTENTE: migraciones_datos.nombre='capas_fase4_eco'.
-- Mantiene contratos.categoria_it / relevancia_ia alineados mientras los
-- lectores siguen leyendo contratos. Se retira en fase 6 (DROP columnas).
--
-- No hay trigger inverso en contratos -> clasificacion (verificado 6 sep):
-- solo trg_texto_busqueda (BEFORE INSERT/UPDATE). Sin bucle.
--
-- Ejecutar: uv run python scripts/run_sql.py docs/capas_fase4_eco.sql

BEGIN;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'capas_fase4_eco'
  ) THEN
    RAISE NOTICE 'capas_fase4_eco ya aplicada. No-op.';
    RETURN;
  END IF;

  -- Defensa: si alguien agrego un trigger inverso, abortar.
  IF EXISTS (
    SELECT 1
    FROM information_schema.triggers
    WHERE event_object_schema = 'public'
      AND event_object_table = 'contratos'
      AND action_statement ILIKE '%clasificacion_contrato%'
  ) THEN
    RAISE EXCEPTION
      'capas_fase4_eco: hay un trigger en contratos que menciona clasificacion_contrato; aborto para evitar bucle';
  END IF;
END $$;

CREATE OR REPLACE FUNCTION public.fn_clasificacion_echo()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    -- Si se borra la fila de capa 3, limpia el eco en contratos.
    UPDATE public.contratos
       SET categoria_it  = NULL,
           relevancia_ia = NULL
     WHERE id = OLD.contrato_id;
    RETURN OLD;
  END IF;

  UPDATE public.contratos
     SET categoria_it  = NEW.categoria_it,
         relevancia_ia = NEW.relevancia_ia
   WHERE id = NEW.contrato_id
     AND (
       categoria_it  IS DISTINCT FROM NEW.categoria_it
       OR relevancia_ia IS DISTINCT FROM NEW.relevancia_ia
     );
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_clasificacion_echo ON public.clasificacion_contrato;
CREATE TRIGGER trg_clasificacion_echo
  AFTER INSERT OR UPDATE OF categoria_it, relevancia_ia
  OR DELETE
  ON public.clasificacion_contrato
  FOR EACH ROW
  EXECUTE FUNCTION public.fn_clasificacion_echo();

COMMENT ON FUNCTION public.fn_clasificacion_echo() IS
  'Capas fase 4: eco temporal clasificacion_contrato -> contratos. Retirar en fase 6.';

INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
SELECT
  'capas_fase4_eco',
  0,
  jsonb_build_object(
    'trigger', 'trg_clasificacion_echo',
    'function', 'fn_clasificacion_echo',
    'nota', 'eco AFTER INSERT/UPDATE/DELETE; sin trigger inverso en contratos'
  )
WHERE NOT EXISTS (
  SELECT 1 FROM migraciones_datos WHERE nombre = 'capas_fase4_eco'
);

COMMIT;
