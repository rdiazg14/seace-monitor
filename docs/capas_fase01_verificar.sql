-- Read-only. Verifica fase 0+1. No escribe.
DO $$
DECLARE
  n_snap int;
  n_cat int;
  n_items int;
  n_docs int;
  n_claf int;
  n_cand int;
  n_anal int;
  m0 boolean;
  m1 boolean;
BEGIN
  SELECT EXISTS (SELECT 1 FROM migraciones_datos WHERE nombre = 'capas_fase0_snapshot') INTO m0;
  SELECT EXISTS (SELECT 1 FROM migraciones_datos WHERE nombre = 'capas_fase1_tablas') INTO m1;
  SELECT count(*) INTO n_snap FROM categoria_it_snapshot_capas;
  SELECT count(*) INTO n_cat FROM categoria_it_snapshot_capas WHERE categoria_it_antes IS NOT NULL;
  SELECT count(*) INTO n_items FROM contrato_items;
  SELECT count(*) INTO n_docs FROM documentos;
  SELECT count(*) INTO n_claf FROM clasificacion_contrato;
  SELECT count(*) INTO n_cand FROM keyword_candidatas;
  SELECT count(*) INTO n_anal FROM analisis_contrato;
  RAISE NOTICE 'marcadores fase0=% fase1=%', m0, m1;
  RAISE NOTICE 'snapshot_filas=% snapshot_categoria_it=%', n_snap, n_cat;
  RAISE NOTICE 'contrato_items=% documentos=% clasificacion_contrato=% keyword_candidatas=% analisis_contrato=%',
    n_items, n_docs, n_claf, n_cand, n_anal;
END $$;
