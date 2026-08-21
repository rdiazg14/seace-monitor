# seace-monitor

Pipeline diario del **SEACE Monitor** (contrataciones menores del Estado peruano).

**Punto de entrada para retomar el producto:** [`docs/TRASPASO_MAESTRO_SEACE.md`](docs/TRASPASO_MAESTRO_SEACE.md).

El README de abajo describe el scraper CSV original (`scraper_seace_menores.py` / `data/contratos_token.csv`). El job de producción es **`.github/workflows/pipeline.yml` a las 09:00 Perú**, no el scrape de las 06:00.

---

Monitoreo automático de **contrataciones menores del Estado peruano (SEACE)**.

Cada día un GitHub Action scrapea el buscador público del SEACE, guarda los
resultados en un CSV dentro de este repo y clasifica cada convocatoria por su
relevancia respecto a **IA / "token"**.

- Fuente: <https://prod6.seace.gob.pe/buscador-publico/contrataciones>
  (SPA de Angular; los datos vienen de una API interna JSON, es público y sin login).
- Salida: [`data/contratos_token.csv`](data/contratos_token.csv)
- CSV crudo (para leer desde cualquier lado):
  `https://raw.githubusercontent.com/rdiazg14/seace-monitor/main/data/contratos_token.csv`

## Cómo funciona el scraper

`scraper_seace_menores.py`:

1. Abre la URL con Playwright (chromium) — arranca la SPA y toma la sesión.
2. Escribe la palabra clave en el buscador y, opcionalmente, marca el filtro
   **Objeto**; da clic en **Buscar**. Esa llamada real a la API se intercepta
   con `page.on("response", ...)`.
3. Vía principal: re-ejecuta la misma API interna con `page.request`, paginando
   de a 100 (la API expone el total en `pageable.totalElements`).
4. Respaldo por DOM si la intercepción/API no capturan nada.
5. Deduplica por `idContrato`, filtra por palabra clave (insensible a
   tildes/mayúsculas) y **clasifica relevancia**:
   - **ALTA**: `token`, `openai`, `azure openai`, `gpt`, `llm`, `claude`,
     `copilot`, `gemini`.
   - **MEDIA**: 2+ términos genéricos de IA (inteligencia artificial, ia
     generativa, chatbot, asistente virtual, machine learning, etc.).
   - **BAJA**: 1 término genérico.

### Uso local

```bash
pip install -r requirements.txt
playwright install chromium
python scraper_seace_menores.py --keyword token --objeto Servicio --out data/contratos_token.csv
```

Parámetros: `--keyword`, `--objeto` (Bien|Servicio|Obra|"Consultoria de Obra"),
`--entidad`, `--out`, `--anio`, `--headed`.

## Automatización

`.github/workflows/scrape.yml` corre a diario (11:00 UTC = 06:00 Perú) y también
manualmente (`workflow_dispatch`). Commitea `data/` de vuelta al repo.

> Nota: la API exige `anio` pero **no filtra por ese año**; se usa el año actual
> solo para satisfacer el parámetro.
