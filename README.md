# seace-monitor

Pipeline Python de SEACE Monitor. Ingiere contrataciones, refresca estados, descarga y extrae documentos, clasifica oportunidades, genera chunks/embeddings y registra observabilidad en Supabase.

## Desarrollo

Requiere Python 3.12 y dependencias bloqueadas por `uv.lock`.

```powershell
uv sync --frozen
uv run pytest -q
```

Las pruebas no arrancan el pipeline. Los entrypoints de ingesta, OCR, clasificación, embeddings, backfill y `scripts/run_sql.py` pueden escribir datos o consumir APIs; no usarlos como comprobaciones de importación.

## Automatización

- `.github/workflows/pipeline.yml`: pipeline diario.
- `.github/workflows/deteccion_temprana.yml`: detección incremental.
- `.github/workflows/clasificacion_semanal.yml`: clasificación periódica.
- `.github/workflows/ci.yml`: pruebas locales reproducibles, sin ejecutar trabajos productivos.

Variables habituales: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `DATABASE_URL`, credenciales del proveedor IA y `ANALIZAR_SERVICE_TOKEN`. Guardar valores únicamente en almacenes de secretos o archivos locales ignorados.

En el workspace completo, arquitectura, datos, operación y pendientes están en `../docs/`. Este README no mantiene el backlog global.
