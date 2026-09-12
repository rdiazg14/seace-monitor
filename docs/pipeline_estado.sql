-- Estado del pipeline: fila única (id=1) que el pipeline actualiza al terminar.
-- El front (Ruta del día) la lee para mostrar "Actualizado: <fecha/hora>".
-- Escritura: service role (marcar_corrida.py), sin política RLS (bypass).
-- Lectura: anon/authenticated vía política SELECT.

CREATE TABLE IF NOT EXISTS public.pipeline_estado (
    id                 integer PRIMARY KEY CHECK (id = 1),
    ultima_corrida_utc timestamptz NOT NULL,
    contratos_total    integer NOT NULL DEFAULT 0,
    contratos_nuevos   integer NOT NULL DEFAULT 0,
    resultado          text NOT NULL DEFAULT 'success'
);

ALTER TABLE public.pipeline_estado ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "pipeline_estado_select" ON public.pipeline_estado;
CREATE POLICY "pipeline_estado_select"
    ON public.pipeline_estado
    FOR SELECT
    TO anon, authenticated
    USING (true);

GRANT SELECT ON public.pipeline_estado TO anon, authenticated;

-- Bootstrap: deja una fila válida para que el encabezado no quede vacío
-- antes de la próxima corrida diaria.
INSERT INTO public.pipeline_estado (id, ultima_corrida_utc, contratos_total, contratos_nuevos, resultado)
VALUES (1, now(), 0, 0, 'success')
ON CONFLICT (id) DO NOTHING;
