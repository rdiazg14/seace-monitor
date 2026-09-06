-- Seguridad: RLS en snapshots y migraciones_datos.
-- IDEMPOTENTE: migraciones_datos.nombre='sec_rls_faltante'.
--
-- Cierra la brecha: tablas de control sin RLS + grants a anon (la anon key
-- viaja en el bundle). Patron = it_keywords: SELECT authenticated + es_admin(),
-- sin policies de escritura (solo service_role / conexion directa).
--
-- Ejecutar: uv run python scripts/run_sql.py docs/sec_rls_faltante.sql

BEGIN;

DO $$
DECLARE
  n_rls int;
BEGIN
  IF EXISTS (
    SELECT 1 FROM migraciones_datos WHERE nombre = 'sec_rls_faltante'
  ) THEN
    SELECT count(*)::int INTO n_rls
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relname IN (
        'categoria_it_snapshot_c2',
        'migraciones_datos',
        'categoria_it_snapshot_capas'
      )
      AND c.relrowsecurity;
    RAISE NOTICE 'sec_rls_faltante ya aplicada. No-op. rls_on=%', n_rls;
    RETURN;
  END IF;

  -- ── categoria_it_snapshot_c2 ───────────────────────────────────────
  ALTER TABLE public.categoria_it_snapshot_c2 ENABLE ROW LEVEL SECURITY;

  REVOKE ALL ON TABLE public.categoria_it_snapshot_c2 FROM anon;
  REVOKE ALL ON TABLE public.categoria_it_snapshot_c2 FROM authenticated;

  GRANT SELECT ON TABLE public.categoria_it_snapshot_c2 TO authenticated;

  DROP POLICY IF EXISTS snapshot_c2_select_admin ON public.categoria_it_snapshot_c2;
  CREATE POLICY snapshot_c2_select_admin
    ON public.categoria_it_snapshot_c2
    FOR SELECT
    TO authenticated
    USING (public.es_admin());

  COMMENT ON POLICY snapshot_c2_select_admin ON public.categoria_it_snapshot_c2 IS
    'Solo perfiles.rol = admin (es_admin). Escritura: service_role / pipeline.';

  -- ── migraciones_datos ──────────────────────────────────────────────
  ALTER TABLE public.migraciones_datos ENABLE ROW LEVEL SECURITY;

  REVOKE ALL ON TABLE public.migraciones_datos FROM anon;
  REVOKE ALL ON TABLE public.migraciones_datos FROM authenticated;

  GRANT SELECT ON TABLE public.migraciones_datos TO authenticated;

  DROP POLICY IF EXISTS migraciones_datos_select_admin ON public.migraciones_datos;
  CREATE POLICY migraciones_datos_select_admin
    ON public.migraciones_datos
    FOR SELECT
    TO authenticated
    USING (public.es_admin());

  COMMENT ON POLICY migraciones_datos_select_admin ON public.migraciones_datos IS
    'Solo perfiles.rol = admin (es_admin). Escritura: service_role / pipeline.';

  -- ── categoria_it_snapshot_capas (ya tenia RLS; cerrar grants) ───────
  ALTER TABLE public.categoria_it_snapshot_capas ENABLE ROW LEVEL SECURITY;

  REVOKE ALL ON TABLE public.categoria_it_snapshot_capas FROM anon;
  REVOKE ALL ON TABLE public.categoria_it_snapshot_capas FROM authenticated;

  GRANT SELECT ON TABLE public.categoria_it_snapshot_capas TO authenticated;

  DROP POLICY IF EXISTS snapshot_capas_select_admin ON public.categoria_it_snapshot_capas;
  CREATE POLICY snapshot_capas_select_admin
    ON public.categoria_it_snapshot_capas
    FOR SELECT
    TO authenticated
    USING (public.es_admin());

  COMMENT ON POLICY snapshot_capas_select_admin ON public.categoria_it_snapshot_capas IS
    'Solo perfiles.rol = admin (es_admin). Escritura: service_role / pipeline.';

  INSERT INTO migraciones_datos (nombre, filas_afectadas, detalle)
  VALUES (
    'sec_rls_faltante',
    3,
    jsonb_build_object(
      'tablas', jsonb_build_array(
        'categoria_it_snapshot_c2',
        'migraciones_datos',
        'categoria_it_snapshot_capas'
      ),
      'accion', 'ENABLE RLS + REVOKE anon/authenticated + SELECT es_admin'
    )
  );

  RAISE NOTICE 'sec_rls_faltante: RLS + revoke + policy admin en 3 tablas';
END $$;

NOTIFY pgrst, 'reload schema';

COMMIT;
