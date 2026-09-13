-- Resultado final de la contratación (menores ≤ 8 UIT).
-- El estado del contrato (Vigente / En Evaluación / Culminado) NO distingue el
-- desenlace: "Desierto", "Anulado" y "Adjudicado" terminan todos como
-- "Culminado" (idEstadoContrato=4) a nivel de contrato. El desenlace real vive
-- a nivel de ÍTEM en el detalle (listar-completo):
--   uitContratoItemProjectionList[].nomEstadoCotiza  = "DESIERTO" | ...
--   uitContratoItemProjectionList[].codRuc / nomRazonSocial / precioTotal
--   (proveedor ganador y monto, solo cuando hubo adjudicación).
--
-- Estas columnas las escribe capturar_resultado.py para contratos IT culminados.
ALTER TABLE public.contratos
    ADD COLUMN IF NOT EXISTS resultado          text,   -- 'DESIERTO' | 'ADJUDICADO' | NULL
    ADD COLUMN IF NOT EXISTS proveedor_ganador  text,   -- nomRazonSocial ganador
    ADD COLUMN IF NOT EXISTS ruc_ganador        text,   -- codRuc ganador
    ADD COLUMN IF NOT EXISTS monto_adjudicado   numeric, -- precioTotal ganador
    ADD COLUMN IF NOT EXISTS resultado_cargado  boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_contratos_resultado
  ON public.contratos (resultado)
  WHERE resultado IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_contratos_resultado_cargado
  ON public.contratos (resultado_cargado)
  WHERE resultado_cargado = false;
