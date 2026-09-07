-- Capas fase 5b: v_contratos con security_invoker (PG15+).
-- Sin esto la vista corre como owner y bypasea RLS de las tablas base.
-- contratos y clasificacion_contrato ya tienen SELECT publico (true);
-- el invoker no cambia el dataset expuesto hoy, pero alinea el modelo
-- de seguridad para cuando las politicas se endurezcan.

ALTER VIEW public.v_contratos SET (security_invoker = true);

COMMENT ON VIEW public.v_contratos IS
  'Fase 5: mismos campos que contratos; cat/relevancia desde capa 3; security_invoker=true.';

NOTIFY pgrst, 'reload schema';
