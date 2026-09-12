-- Contador de cuota Flash de OCR (una fila por día Lima).
-- Reemplaza data/flash_ocr_cuota.json como fuente de verdad: así el pipeline
-- diario y la detección temprana (cada 2 h) comparten el mismo tope sin
-- pisarse por un archivo git que solo commitea el diario.
-- Escritura: service role (descargar_requerimiento.py). Lectura: solo service
-- role (dato interno; no se expone al front).
CREATE TABLE IF NOT EXISTS public.pipeline_cuota_ocr (
    fecha_lima    date PRIMARY KEY,
    requests      integer NOT NULL DEFAULT 0,
    prompt_tokens bigint NOT NULL DEFAULT 0,
    out_tokens    bigint NOT NULL DEFAULT 0,
    usd_est       numeric(18, 6) NOT NULL DEFAULT 0,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.pipeline_cuota_ocr ENABLE ROW LEVEL SECURITY;
