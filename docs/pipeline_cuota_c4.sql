-- Contador de cupo de Gemini C4 (clasificación semanal; una fila por día Lima).
-- Reemplaza data/clasificacion_cuota.json como fuente de verdad: así el diario
-- y el semanal comparten el mismo tope sin pisarse por un archivo git que solo
-- commitea el semanal.
-- Escritura: service role (clasificar_gemini.py). Lectura: solo service role
-- (dato interno; no se expone al front).
CREATE TABLE IF NOT EXISTS public.pipeline_cuota_c4 (
    fecha_lima        date PRIMARY KEY,
    requests          integer NOT NULL DEFAULT 0,
    prompt_tokens     bigint NOT NULL DEFAULT 0,
    candidates_tokens bigint NOT NULL DEFAULT 0,
    total_tokens      bigint NOT NULL DEFAULT 0,
    updated_at        timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.pipeline_cuota_c4 ENABLE ROW LEVEL SECURITY;

NOTIFY pgrst, 'reload schema';
