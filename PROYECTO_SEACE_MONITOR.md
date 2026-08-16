# SEACE Monitor — Documento de proyecto completo

> **Briefing para un ingeniero nuevo.** Este documento es autocontenido: quien lo lea puede entender,
> operar y mejorar el sistema sin hacer preguntas adicionales.
>
> URL de producción: **https://seace.rdiaz-lab.xyz**
> Última validación: **2026-08-15** (queries y llamadas reales ejecutadas ese día)

---

## Tabla de contenidos

1. [Qué es y para qué sirve](#1-qué-es-y-para-qué-sirve)
2. [Arquitectura](#2-arquitectura)
3. [Repositorios y URLs](#3-repositorios-y-urls)
4. [Base de datos — schema completo](#4-base-de-datos--schema-completo)
5. [Pipeline de datos](#5-pipeline-de-datos)
6. [RAG — cómo funciona la búsqueda inteligente](#6-rag--cómo-funciona-la-búsqueda-inteligente)
7. [Frontend](#7-frontend)
8. [API pública](#8-api-pública)
9. [Métricas actuales](#9-métricas-actuales)
10. [Problemas conocidos y mejoras pendientes](#10-problemas-conocidos-y-mejoras-pendientes)
11. [Cómo operar el sistema](#11-cómo-operar-el-sistema)
12. [Historial de decisiones técnicas](#12-historial-de-decisiones-técnicas)

---

## 1. Qué es y para qué sirve

**SEACE Monitor** es una plataforma de monitoreo automático de contrataciones públicas del Estado peruano. Descarga diariamente el corpus de contrataciones menores publicadas en el SEACE (Sistema Electrónico de Contrataciones del Estado), clasifica cada contrato por categoría tecnológica, lo expone mediante un dashboard web con buscador, y permite hacer preguntas en lenguaje natural que el sistema responde usando los Términos de Referencia reales almacenados en una base de datos vectorial (RAG).

**Usuario objetivo:** ENERTRONIC INGENIERÍA S.A.C., empresa peruana de ingeniería que busca oportunidades de contratación con el Estado en las categorías de hardware, ciberseguridad, redes, equipos informáticos y afines.

**Problema que resuelve:** El SEACE publica miles de contratos diarios en una SPA sin API pública documentada. Un proveedor que quiere encontrar oportunidades tiene que navegar manualmente una interfaz lenta, sin filtros de categoría tecnológica y sin capacidad de búsqueda semántica. SEACE Monitor automatiza todo el pipeline: descarga → clasifica → indexa → expone con búsqueda inteligente.

**Costo:** $0/mes. Usa exclusivamente free tiers: GitHub Actions, GitHub Pages, Supabase Free, Cloudflare Workers Free.

---

## 2. Arquitectura

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          FUENTE EXTERNA                                  │
│  seace.gob.pe — SPA pública, sin auth — API JSON interna paginada       │
│  URL buscador:  https://prod6.seace.gob.pe/buscador-publico/contratacios │
│  API JSON:      https://prod6.seace.gob.pe/v1/s8uit-services/...        │
│  API detalle:   ...buscadorpublico/contrataciones/listar-completo        │
└──────────────────────────────┬───────────────────────────────────────────┘
                               │ Playwright (Chromium headless)
                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│              GITHUB ACTIONS  (rdiazg14/seace-monitor)                    │
│  cron: 0 11 * * *  → 06:00 AM Perú (UTC-5)                              │
│                                                                          │
│  [1] ingesta_completa.py   → corpus 76k registros, UPSERT a Supabase    │
│  [2] enriquecer_detalle.py → API listar-completo (solo vigentes)         │
│  [3] chunker_contratos.py  → genera chunks TDR (descripcion+items)       │
│  [4] generar_embeddings.py → BGE-base-en-v1.5 vía CF Worker /embed      │
│  [5] auto-commit data/     → timestamps + stats                          │
└──────────────────────────────┬───────────────────────────────────────────┘
                               │ UPSERT (service_role key)
                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│            SUPABASE PostgreSQL + pgvector                                │
│            wusywwhcyqngnpvpzxyr.supabase.co  (sa-east-1, São Paulo)     │
│                                                                          │
│  tabla contratos      (76,251 filas)  — datos del listado SEACE          │
│  tabla chunks_tdr     (9,077 filas)   — fragmentos TDR + embedding 768d  │
│  func buscar_contratos()              — FTS Spanish + filtros            │
│  func buscar_tdr()                    — búsqueda vectorial coseno        │
│  view dashboard_resumen               — agrupado por mes/objeto/estado   │
│  view vigentes_urgentes               — vigentes ordenados por cierre    │
└──────────┬───────────────────────────────────┬───────────────────────────┘
           │ anon key (pública, solo SELECT)   │ (mismo)
           ▼                                   ▼
┌───────────────────────────┐    ┌─────────────────────────────────────────┐
│  GITHUB PAGES             │    │  CLOUDFLARE WORKER                      │
│  seace.rdiaz-lab.xyz      │    │  seace-ai-proxy.rdiazg14.workers.dev    │
│  rdiazg14/seace-web       │    │                                         │
│                           │    │  1. Recibe query del usuario            │
│  /        Dashboard       │    │  2. Genera embedding (BGE via AI)       │
│  /buscar  Buscador FTS    │────►  3. buscar_tdr() pgvector (sim > 0.70) │
│  /chat    Chat RAG SSE    │    │  4. Si < 3 resultados → FTS compensa    │
│  /docs    API Docs        │    │  5. Llama Llama 3.3 70B (streaming SSE) │
│                           │◄───│  6. Devuelve tokens + contratos_ref     │
└───────────────────────────┘    └─────────────────────────────────────────┘
```

### Los 5 servicios y su rol

| Servicio | Rol | Free tier usado |
|---|---|---|
| **GitHub Actions** | Scraper + pipeline RAG diario (cron 11:00 UTC) | 2,000 min/mes gratis |
| **Supabase PostgreSQL + pgvector** | Base de datos principal, FTS, búsqueda vectorial | 500 MB almacenamiento, 50k rows → usamos 76k (limite row no existe en free) |
| **GitHub Pages** | Hosting del frontend SPA estático | Gratis ilimitado para repos públicos |
| **Cloudflare Workers** | Proxy RAG: embeddings + LLM + CORS + API key segura | 100k requests/día gratis |
| **Cloudflare Workers AI** | Modelo BGE (embeddings) + Llama 3.3 70B (LLM) | Embeddings ilimitados; LLM ~10k tokens/día |

---

## 3. Repositorios y URLs

### GitHub

| Repo | URL | Descripción |
|---|---|---|
| `rdiazg14/seace-monitor` | https://github.com/rdiazg14/seace-monitor | Scraper Python, scripts RAG, GitHub Actions, datos |
| `rdiazg14/seace-web` | https://github.com/rdiazg14/seace-web | Frontend React + CI/CD GitHub Pages |

### Archivos principales de seace-monitor

```
seace-monitor/
├── .env                       # Secretos locales (gitignored)
├── .github/workflows/
│   └── scrape.yml             # Pipeline diario: 5 pasos en secuencia
├── ingesta_completa.py        # Fase 1: descarga corpus SEACE → Supabase
├── enriquecer_detalle.py      # Fase 2: API listar-completo para vigentes
├── chunker_contratos.py       # Fase 3: genera chunks TDR
├── generar_embeddings.py      # Fase 4: llama /embed del Worker → pgvector
├── supabase_schema.sql        # Schema tabla contratos, FTS, vistas, RLS
├── schema_rag.sql             # Schema chunks_tdr, pgvector, buscar_tdr()
├── DOCUMENTACION.md           # Doc técnica completa (versión anterior)
├── PROYECTO_SEACE_MONITOR.md  # Este archivo
├── requirements.txt
└── data/
    ├── seace_menores_completo.parquet   # Backup local snappy
    ├── seace_menores_completo.csv       # CSV (últimas 1000 filas si > 100 MB)
    ├── contratos_token.csv              # Salida scraper filtrado
    ├── ultima_ingesta.txt               # Timestamp + stats de ingesta
    └── ultima_corrida.txt               # Timestamp de la corrida del Action
```

### Archivos principales de seace-web

```
seace-web/src/
├── main.tsx
├── App.tsx                    # BrowserRouter + rutas + layout raíz
├── types.ts                   # Interfaces Contrato, DashboardResumen
├── lib/supabase.ts            # createClient (anon key hardcoded — OK con RLS)
├── components/
│   ├── Navbar.tsx             # Navegación sticky, hamburger mobile
│   └── ContratoCard.tsx       # Tarjeta reutilizable con badges de estado/objeto
└── pages/
    ├── Dashboard.tsx          # Stats, BarChart, PieChart, tabs Vigentes/IT
    ├── Buscador.tsx           # FTS con filtros, paginación
    ├── Chat.tsx               # Chat RAG con SSE streaming
    └── Docs.tsx               # Documentación API con ejemplos curl/Python
```

### Archivos principales de seace-ai-proxy

```
seace-ai-proxy/
├── src/index.ts               # Worker: embed + RAG + SSE + CORS
└── wrangler.toml              # name, AI binding, SUPABASE_URL, SIMILARITY_THRESHOLD
```

### URLs de producción y endpoints

| URL | Qué es |
|---|---|
| `https://seace.rdiaz-lab.xyz` | Frontend web (GitHub Pages, CNAME en Namecheap) |
| `https://wusywwhcyqngnpvpzxyr.supabase.co` | Supabase project URL |
| `https://seace-ai-proxy.rdiazg14.workers.dev` | Cloudflare Worker RAG |
| `https://rdiazg14.github.io` | GitHub Pages base (redirige al CNAME) |

### Credenciales públicas (anon key — solo lectura, seguro incluir)

```
SUPABASE_URL      = https://wusywwhcyqngnpvpzxyr.supabase.co
SUPABASE_ANON_KEY = eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Ind1c3l3d2hjeXFuZ25wdnB6eHlyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODY3NDc0NDcsImV4cCI6MjEwMjMyMzQ0N30.jDZeGaW8lQuROU7IF11clkfjgyyiMrgyIfi6LvuAFeY
```

La service_role key (escritura) vive en `.env` local y en GitHub Secrets. **Nunca en código.**

### DNS (Namecheap)

```
Tipo:   CNAME
Host:   seace
Valor:  rdiazg14.github.io
TTL:    Automático
```

---

## 4. Base de datos — schema completo

### Tabla `contratos`

| Columna | Tipo | Fuente | Descripción |
|---|---|---|---|
| `id` | `BIGINT PK` | API SEACE `idContrato` | Clave primaria, usada como clave de UPSERT |
| `nro_contratacion` | `TEXT` | `nroContratacion` | Número del proceso (ej: "482334") |
| `descripcion_contrato` | `TEXT` | `desContratacion` | Código del proceso (ej: CM-482334-2026-BN) |
| `objeto` | `TEXT` | `nomObjetoContrato` | Bien / Servicio / Obra / Consultoría de Obra |
| `descripcion` | `TEXT` | `desObjetoContrato` | Texto largo del objeto contratado (TDR general) |
| `entidad` | `TEXT` | `nomEntidad` | Nombre de la entidad del Estado |
| `estado` | `TEXT` | `nomEstadoContrato` | Vigente / En Evaluación / Culminado / Cancelado |
| `fecha_publica` | `TIMESTAMPTZ` | `fecPublica` | Fecha de publicación (parseada de dd/mm/yyyy) |
| `fecha_ini_cotizacion` | `TIMESTAMPTZ` | `fecIniCotizacion` | Inicio período cotización |
| `fecha_fin_cotizacion` | `TIMESTAMPTZ` | `fecFinCotizacion` | Cierre cotización (determina urgencia) |
| `tipo_cotizacion` | `TEXT` | `idTipoCotizacion` | ID del tipo de cotización |
| `cotizar` | `BOOLEAN` | `cotizar` | Si acepta cotizaciones actualmente |
| `categoria_it` | `TEXT\|NULL` | Pipeline | 13 categorías IT (asignadas por keyword matching) |
| `relevancia_ia` | `TEXT\|NULL` | Pipeline | ALTA / MEDIA / BAJA (para IA generativa) |
| `texto_busqueda` | `TSVECTOR` | Trigger auto | Índice FTS en español (descripcion+entidad+objeto) |
| `created_at` | `TIMESTAMPTZ` | DB default | Timestamp de creación en Supabase |
| `nom_area_usuaria` | `TEXT` | API detalle | Área usuaria (de `listar-completo`) |
| `items_json` | `JSONB` | API detalle | Lista de ítems CUBSO (specs técnicas) |
| `detalle_cargado` | `BOOLEAN` | Pipeline | True si ya se llamó a `listar-completo` |

### Tabla `chunks_tdr`

| Columna | Tipo | Descripción |
|---|---|---|
| `id` | `BIGSERIAL PK` | Clave primaria autoincremental |
| `contrato_id` | `BIGINT FK` | Referencia a `contratos.id` (ON DELETE CASCADE) |
| `chunk_index` | `SMALLINT` | Índice del chunk dentro del contrato (0-based) |
| `tipo` | `TEXT` | "Descripción general" / "Ítem técnico N" / "Metadata" |
| `texto` | `TEXT` | Contenido del chunk (truncado a 2000 chars para embedding) |
| `embedding` | `vector(768)` | Embedding BGE-base-en-v1.5 (768 dimensiones) |
| `created_at` | `TIMESTAMPTZ` | Timestamp de creación |

Constraint: `UNIQUE(contrato_id, chunk_index)`

### Función `buscar_tdr()`

```sql
CREATE OR REPLACE FUNCTION buscar_tdr(
  query_embedding vector(768),
  match_count     INT     DEFAULT 10,
  filter_estado   TEXT    DEFAULT NULL,
  min_similarity  FLOAT   DEFAULT 0.80   -- el Worker pasa 0.70 en env var
)
RETURNS TABLE (
  contrato_id   BIGINT,
  chunk_index   SMALLINT,
  tipo          TEXT,
  texto         TEXT,
  similarity    FLOAT
)
```
Búsqueda por similitud coseno (`<=>` operator de pgvector). Filtra por estado opcionalmente. Devuelve los `match_count` más similares con `similarity > min_similarity`.

### Función `buscar_contratos()`

```sql
CREATE OR REPLACE FUNCTION buscar_contratos(
  termino         TEXT  DEFAULT '',
  filtro_objeto   TEXT  DEFAULT NULL,
  filtro_estado   TEXT  DEFAULT NULL,
  filtro_entidad  TEXT  DEFAULT NULL,
  limite          INT   DEFAULT 50,
  offset_val      INT   DEFAULT 0
)
RETURNS TABLE (... todos los campos de contratos + rank REAL ...)
```
Full-text search en español (`plainto_tsquery('spanish', termino)`) con ranking `ts_rank`. Acepta filtros de objeto, estado y entidad (ILIKE parcial).

### Vista `dashboard_resumen`

```sql
SELECT objeto, estado, categoria_it,
       DATE_TRUNC('month', fecha_publica)::DATE AS mes,
       COUNT(*)::INT AS total
FROM contratos
GROUP BY objeto, estado, categoria_it, mes
```
Usada por el Dashboard para calcular las stat cards y las gráficas Recharts.

### Vista `vigentes_urgentes`

```sql
SELECT * FROM contratos
WHERE estado = 'Vigente'
ORDER BY fecha_fin_cotizacion ASC NULLS LAST
```
Contratos abiertos ordenados por cierre más próximo. Usado en la tab "Vigentes" del Dashboard.

### Índices relevantes

```sql
-- FTS (TSVECTOR generado por trigger)
CREATE INDEX idx_contratos_fts ON contratos USING GIN(texto_busqueda);

-- Vectorial (ANN coseno, lists=100 para ~50k-500k filas)
CREATE INDEX idx_chunks_embedding ON chunks_tdr
  USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Filtros frecuentes
CREATE INDEX idx_contratos_estado ON contratos(estado);
CREATE INDEX idx_contratos_fecha  ON contratos(fecha_publica DESC NULLS LAST);
CREATE INDEX idx_contratos_fecha_fin_vigente ON contratos(fecha_fin_cotizacion)
  WHERE estado = 'Vigente';
```

### RLS (Row Level Security)

```sql
-- contratos: anon puede SELECT todo (datos públicos del Estado peruano)
ALTER TABLE contratos ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Lectura publica" ON contratos
  FOR SELECT TO anon, authenticated USING (true);

-- chunks_tdr: igual, solo lectura pública
ALTER TABLE chunks_tdr ENABLE ROW LEVEL SECURITY;
CREATE POLICY "chunks_tdr_select_anon" ON chunks_tdr
  FOR SELECT USING (true);
```

### Extensiones

```sql
CREATE EXTENSION IF NOT EXISTS vector;  -- pgvector para embeddings 768d
```

### Números actuales (validados 2026-08-15)

| Métrica | Valor |
|---|---|
| Total contratos | **76,251** |
| Vigentes | **2,331** |
| En Evaluación | **16,047** |
| Culminados | **57,873** |
| Con detalle_cargado=true | **2,331** |
| Con items_json | **2,331** |
| Total chunks_tdr | **9,077** |
| Chunks con embedding | **9,077** (100%) |

---

## 5. Pipeline de datos

### Cómo llegan los datos — paso a paso

```
[FASE 1] ingesta_completa.py
  → Playwright lanza Chromium headless
  → Navega la SPA (necesario para obtener cookies de sesión)
  → Llama a la API JSON con page.request.get() (reutiliza cookies)
  → Pagina de a 100 registros hasta el MAX(id) conocido (modo INCREMENTAL)
  → Clasifica cada contrato: categoria_it + relevancia_ia (keyword matching)
  → UPSERT a Supabase en lotes de 500 (conflict: id → update)
  → Guarda backup local parquet + CSV

[FASE 2] enriquecer_detalle.py
  → Solo para contratos con estado='Vigente' y detalle_cargado=false
  → Llama a API listar-completo?id_contrato={id}
  → Extrae: nomAreaUsuaria, lista de ítems CUBSO
  → Actualiza: nom_area_usuaria, items_json, detalle_cargado=true, descripcion

[FASE 3] chunker_contratos.py
  → Solo contratos con detalle_cargado=true que aún no tienen chunks
  → Por cada contrato genera hasta 3 tipos de chunks:
      - "Descripción general": texto desObjetoContrato (split si > 800 tokens)
      - "Ítem técnico N": CUBSO cod + nombre + cantidad + lugar + specs
      - "Metadata": entidad, área usuaria, objeto, estado, número
  → UPSERT a chunks_tdr (conflict: contrato_id, chunk_index)

[FASE 4] generar_embeddings.py
  → Lee chunks_tdr donde embedding IS NULL
  → Llama al Worker /embed en lotes de 20 textos
  → Worker usa @cf/baai/bge-base-en-v1.5 → devuelve 768 floats por texto
  → UPSERT el vector a chunks_tdr.embedding
  → Retry exponencial: 2s, 4s, 8s si falla
```

### API del SEACE descubierta

Por qué se necesita Playwright: la API usa cookies de sesión que solo se obtienen navegando la SPA real. `page.request.get()` reutiliza esas cookies automáticamente.

**API de listado (corpus):**
```
GET https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico/contrataciones/buscador
Params: anio=2026 | palabra_clave= | orden=2 | page=1 | page_size=100
Auth: ninguna (usa cookies de sesión SPA)
```

**Estructura de respuesta:**
```json
{
  "pageable": { "totalElements": 76251, "totalPages": 763 },
  "data": [{
    "idContrato": 86998, "nroContratacion": "...", "desContratacion": "CM-...",
    "desObjetoContrato": "Servicio", "nomObjetoContrato": "Servicio",
    "nomEntidad": "GOBIERNO REGIONAL DE LA LIBERTAD...",
    "nomEstadoContrato": "Vigente",
    "fecPublica": "15/08/2026 00:00:00",
    "fecIniCotizacion": "15/08/2026 08:00:00",
    "fecFinCotizacion": "22/08/2026 17:00:00",
    "idTipoCotizacion": 2, "cotizar": true
  }]
}
```

**API de detalle (TDR):**
```
GET https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico/contrataciones/listar-completo
Params: id_contrato={id}
Respuesta: { "uitContratoCompletoProjection": { "nomAreaUsuaria": "...", "desObjetoContrato": "..." },
             "uitContratoItemProjectionList": [{ "codCubso": "...", "nomCubso": "...",
               "descripcionItem": "...", "cantidad": 1, "nomUnidadMedida": "SERVICIO",
               "nomDistrito": "LIMA" }] }
```

### Clasificación automática — 13 categorías IT

La clasificación usa keyword matching con normalización NFKD (sin tildes, minúsculas). Primera categoría que coincide gana.

| Categoría | Keywords (muestra) |
|---|---|
| Firma digital | firma digital, certificado digital, token criptografico |
| IA/analytics | inteligencia artificial, machine learning, gpt, llm, claude, copilot, gemini, chatbot, big data |
| Ciberseguridad | ciberseguridad, seguridad informatica, firewall, pentest |
| Cloud/hosting | nube publica, cloud computing, hosting, aws, google cloud |
| Microsoft | microsoft, office 365, sharepoint, exchange, windows server |
| Oracle | oracle database, oracle ebs, peoplesoft |
| Base de datos/ERP | base de datos, sql server, postgresql, mysql, sap, erp |
| Desarrollo software | desarrollo de software, sistema de informacion, plataforma web, app movil |
| Licencias | licencia de software, licenciamiento, suscripcion de software |
| Soporte tecnico | soporte tecnico, mantenimiento de software, helpdesk, mesa de ayuda |
| Redes/cableado | red de datos, cableado estructurado, switch, router, fibra optica, wifi |
| Correo electronico | correo electronico, mensajeria electronica |
| Hardware | computadora, laptop, impresora, monitor, disco duro, ups, tablet, scanner |

**Relevancia IA (3 niveles):**
- **ALTA:** cualquier keyword exacto de IA puntual aparece (token, openai, gpt, llm, claude, copilot, gemini, azure openai)
- **MEDIA:** 2+ keywords genéricos de IA (inteligencia artificial, machine learning, chatbot, etc.)
- **BAJA:** exactamente 1 keyword genérico de IA

### Horario del pipeline

`cron: 0 11 * * *` → **11:00 UTC = 06:00 AM Perú (UTC-5)**

---

## 6. RAG — cómo funciona la búsqueda inteligente

### Flujo completo del Worker cuando recibe una pregunta

```
1. POST https://seace-ai-proxy.rdiazg14.workers.dev
   Body: { "query": "contratos de ciberseguridad vigentes" }
   Header: Accept: text/event-stream  (para SSE)

2. Worker: extrae filtros de la query con regex
   - filtroEstado(): /vigent/ → "Vigente"; /culminad|cerrad/ → "Culminado"
   - extraerTermino(): quita stopwords (que, cuales, hay, contratos, etc.)
   - extraerEntidad(): detecta "ministerio de X", "municipalidad X", etc.

3. embedQuery(): llama a env.AI.run("@cf/baai/bge-base-en-v1.5", { text: [termino] })
   → vector float[768]

4. buscarTdr(): POST /rest/v1/rpc/buscar_tdr { query_embedding, match_count: 10,
   filter_estado: null, min_similarity: 0.70 }
   → lista de ChunkHit[] con { contrato_id, chunk_index, tipo, texto, similarity }

5. Si chunks.length < 3 → buscarContratosFts() complementa con FTS clásico
   POST /rest/v1/rpc/buscar_contratos { termino, filtro_estado, filtro_entidad, limite: 6 }

6. fetchContratos() → obtiene metadata completa de los contratos referenciados
   GET /rest/v1/contratos?id=in.(id1,id2,...)

7. Arma fragmentos de contexto (uno por chunk + uno por FTS):
   "Contrato: CM-xxx\nEntidad: ...\nEstado: ...\nÁrea: ...\nSección: ...\nContenido: ...\nLink: ..."

8. Llama a env.AI.run("@cf/meta/llama-3.3-70b-instruct-fp8-fast",
   { messages: [system_prompt, user_prompt], max_tokens: 1024, stream: true })

9. Lee el stream SSE del LLM, re-emite cada token como SSE event al cliente:
   data: {"stage":"streaming","token":"..."}

10. Al terminar envía:
    data: {"stage":"done","contratos_referenciados":[...],"chunks_usados":N}
```

### System prompt del LLM (completo)

```
Eres un asistente experto en contrataciones públicas del Estado peruano (SEACE). Respondes siempre en español.

Tienes acceso a los Términos de Referencia (TDR) reales de los contratos. Abajo se proporcionan fragmentos relevantes.

Reglas:
- Responde basándote SOLO en los fragmentos proporcionados
- Cita siempre la entidad y el número de contrato
- Incluye detalles técnicos específicos (specs, cantidades, CUBSO)
- Si hay requisitos, plazos o penalidades, menciónalos
- Prioriza contratos en estado Vigente
- Al final de cada contrato, incluye el link al SEACE
- Si no hay información relevante, dilo claramente
- No inventes datos que no estén en los fragmentos
```

### Fallback híbrido: vector + FTS

```
Si buscar_tdr() devuelve ≥ 3 chunks → solo vector, sin FTS
Si buscar_tdr() devuelve < 3 chunks → complementa con buscar_contratos() FTS
```
Razón: BGE-base-en-v1.5 es un modelo entrenado en inglés. Para texto en español su similitud es más baja, así que sin el fallback FTS habría queries sin resultados.

### Configuración actual

```toml
# wrangler.toml
SIMILARITY_THRESHOLD = "0.70"
```

El código fuente tiene 0.80 como default hardcoded, pero el env var lo sobreescribe a 0.70. Con 0.70 los resultados son más inclusivos, necesario para compensar que BGE no es multilingüe.

### Limitaciones conocidas

- **BGE inglés sobre español**: similitud coseno más baja de lo ideal. Los embeddings funcionan pero con ruido. El fallback FTS mitiga el problema.
- **Chunks cortos**: promedio ~46 tokens (estimado). Poco contexto por chunk. Mejora futura: texto más largo por chunk.
- **Sin PDFs**: el SEACE no expone PDFs vía API para contrataciones menores. Todo el texto viene de `desObjetoContrato` (texto de la API) + ítems CUBSO.
- **Latencia**: 4-14 segundos totales. Con SSE el primer token llega en ~0.5s.

### Modelos usados

```
Embeddings: @cf/baai/bge-base-en-v1.5  (768 dims, max 512 tokens por texto)
LLM:        @cf/meta/llama-3.3-70b-instruct-fp8-fast  (max_tokens: 1024)
```

---

## 7. Frontend

### Stack

| Tecnología | Versión | Rol |
|---|---|---|
| Vite | ^8.2.0 | Build tool |
| React | ^19.2.8 | UI framework |
| TypeScript | ~6.0.2 | Tipado estático |
| Tailwind CSS | ^3.4.19 | Estilos utility-first |
| Recharts | ^3.10.1 | Gráficas dashboard |
| @supabase/supabase-js | ^2.112.3 | Cliente Supabase |
| react-router-dom | ^7.18.2 | SPA routing |

### Páginas (las 4 rutas)

**`/` — Dashboard (briefing operativo)**
- 4 stat cards: Total contratos / Vigentes / Con categoría IT / IA/analytics
- BarChart: contratos por mes (de `dashboard_resumen`)
- PieChart: distribución Bien/Servicio/Obra
- Tab "Vigentes": listado de `vigentes_urgentes` con cierre próximo
- Tab "IT": contratos con `categoria_it` ordenados por fecha

**`/buscar` — Buscador FTS**
- Input de texto libre → llama `buscar_contratos()` con FTS español
- Filtros: objeto (Bien/Servicio/Obra/Consultoría) + estado (Vigente/En Evaluación/Culminado)
- Resultados paginados de 20 en 20 con `ContratoCard`

**`/chat` — Chat RAG con SSE**
- Envía POST al Worker con `Accept: text/event-stream`
- Muestra tokens streameando en tiempo real (primer token ~0.5s)
- Al final muestra lista de contratos referenciados como `ContratoCard`
- Sugerencias rápidas cuando no hay historial

**`/docs` — Documentación API**
- Referencia completa de endpoints con ejemplos curl y Python
- Prompts listos para usar en Claude Projects o ChatGPT

### SPA routing en GitHub Pages

```
public/404.html = copia de index.html
```
GitHub Pages retorna 404 cuando se accede directamente a `/chat` o `/buscar`. La solución es un `404.html` que carga la SPA y React Router maneja el routing del lado cliente.

### Variables de entorno

```
VITE_SUPABASE_URL      = https://wusywwhcyqngnpvpzxyr.supabase.co
VITE_SUPABASE_ANON_KEY = eyJhbGci... (anon key, OK en frontend)
VITE_WORKER_URL        = https://seace-ai-proxy.rdiazg14.workers.dev
```

**Nota:** La anon key está actualmente hardcoded en `src/lib/supabase.ts`. Funciona con RLS porque solo da SELECT. Si se migra a variables de entorno Vite, recordar que `VITE_*` vars se embeben en el bundle (igualmente públicas).

### Deploy

Cualquier push a `main` de `rdiazg14/seace-web` dispara `deploy.yml`:
1. `npm ci` → `npm run build` (`tsc -b && vite build`) → `dist/`
2. Sube `dist/` a GitHub Pages
3. En producción en ~2 minutos

---

## 8. API pública

### Autenticación

```
Base URL: https://wusywwhcyqngnpvpzxyr.supabase.co
Headers requeridos:
  apikey: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Ind1c3l3d2hjeXFuZ25wdnB6eHlyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODY3NDc0NDcsImV4cCI6MjEwMjMyMzQ0N30.jDZeGaW8lQuROU7IF11clkfjgyyiMrgyIfi6LvuAFeY
  Authorization: Bearer [misma key]
  Content-Type: application/json
```

### Endpoints

**Búsqueda FTS:**
```bash
curl -X POST 'https://wusywwhcyqngnpvpzxyr.supabase.co/rest/v1/rpc/buscar_contratos' \
  -H "apikey: $KEY" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"termino":"ciberseguridad","filtro_objeto":"Servicio","filtro_estado":"Vigente",
       "filtro_entidad":null,"limite":10,"offset_val":0}'
```

**Búsqueda semántica (requiere embedding):**
```bash
curl -X POST 'https://wusywwhcyqngnpvpzxyr.supabase.co/rest/v1/rpc/buscar_tdr' \
  -H "apikey: $KEY" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"query_embedding":[...768 floats...],"match_count":5,"min_similarity":0.70}'
```

**Vista dashboard:**
```bash
curl 'https://wusywwhcyqngnpvpzxyr.supabase.co/rest/v1/dashboard_resumen' \
  -H "apikey: $KEY" -H "Authorization: Bearer $KEY"
```

**Vista vigentes urgentes:**
```bash
curl 'https://wusywwhcyqngnpvpzxyr.supabase.co/rest/v1/vigentes_urgentes?limit=20' \
  -H "apikey: $KEY" -H "Authorization: Bearer $KEY"
```

**Worker RAG (streaming SSE):**
```bash
curl -N -X POST 'https://seace-ai-proxy.rdiazg14.workers.dev' \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"query":"contratos de ciberseguridad vigentes"}'
```

### Prompts para Claude Projects

```
System prompt para Claude Projects:
Eres un asistente especializado en contrataciones públicas del Estado peruano (SEACE).

Endpoint de búsqueda:
POST https://wusywwhcyqngnpvpzxyr.supabase.co/rest/v1/rpc/buscar_contratos
Headers:
  apikey: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Ind1c3l3d2hjeXFuZ25wdnB6eHlyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODY3NDc0NDcsImV4cCI6MjEwMjMyMzQ0N30.jDZeGaW8lQuROU7IF11clkfjgyyiMrgyIfi6LvuAFeY
  Authorization: Bearer [mismo]
  Content-Type: application/json
Body: { "termino": "texto", "filtro_objeto": null, "filtro_estado": null,
        "filtro_entidad": null, "limite": 10, "offset_val": 0 }

Instrucciones:
1. Llama al endpoint cuando el usuario pregunte sobre contratos.
2. Presenta: entidad, descripción, estado, fecha de cierre.
3. Prioriza contratos Vigentes con cierre próximo.
4. Responde siempre en español.
```

---

## 9. Métricas actuales

**Fecha de validación:** 2026-08-15 (queries ejecutadas en vivo)

### Estado de Supabase

| Métrica | Valor |
|---|---|
| Total contratos | 76,251 |
| Vigentes | 2,331 |
| En Evaluación | 16,047 |
| Culminados | 57,873 |
| Con detalle_cargado=true | 2,331 |
| Con items_json | 2,331 |
| Total chunks_tdr | 9,077 |
| Chunks con embedding (100%) | 9,077 |
| Chunks "Descripción general" | ~242 (mayoría) |
| Chunks "Metadata" | ~237 (uno por contrato) |
| Chunks "Ítem técnico N" | ~8,598 (suma de todos los ítems) |

*El 100% de los contratos vigentes tienen detalle y embeddings generados.*

### Performance del RAG Worker

| Query | Chunks encontrados | Contratos ref. | Tiempo total | Calidad |
|---|---|---|---|---|
| "contratos de ciberseguridad vigentes" | 5 (solo vector) | 5 | 5,954ms | BUENA — cita CUBSO, entidad, link |
| "adquisicion de equipos de computo" | 10 (solo vector) | >3 | 4,041ms | BUENA — 10 hits vectoriales |
| "servicios de cloud o nube" | 3 (solo vector) | 3 | 10,675ms | BUENA — encontró SUTRAN, UNPRG |

Latencia al primer token SSE: ~0.5s (mensaje "searching" inmediato).

### GitHub Actions — últimas corridas

| Fecha UTC | Trigger | Estado | Duración |
|---|---|---|---|
| 2026-08-15 19:45:39 | workflow_dispatch | ✅ success | 1m 44s |
| 2026-08-15 11:12:26 | schedule (cron) | ✅ success | 1m 16s |
| 2026-08-14 18:19:05 | workflow_dispatch | ✅ success | 1m 8s |
| 2026-08-14 15:14:49 | workflow_dispatch | ✅ success | 1m 5s |

Tiempo promedio de corrida: **~1m 20s**. Todas en verde. El pipeline incremental es rápido porque en días normales solo hay 0-5 contratos nuevos.

### Última ingesta (2026-08-15 19:46:54 UTC)

```
total_registros=76,251
nuevos_esta_corrida=1
```

---

## 10. Problemas conocidos y mejoras pendientes

### BGE inglés sobre texto español
**Problema:** `@cf/baai/bge-base-en-v1.5` fue entrenado en inglés. Para texto en español la similitud coseno es subóptima — queries en español producen embeddings que no capturan semántica con la misma fidelidad.
**Impacto:** Algunas búsquedas semánticas devuelven < 3 chunks y activa el fallback FTS.
**Solución futura:** Migrar a `multilingual-e5-large` (si llega a Workers AI gratis) o `bge-m3` (multilingüe). Requeriría regenerar todos los embeddings.
**Mitigación actual:** Fallback híbrido vector+FTS cuando chunks < 3.

### Chunks cortos (~46 tokens promedio)
**Problema:** Los chunks de "Ítem técnico" son muy cortos (solo CUBSO + cantidad + lugar + specs en 1 línea). Poco contexto para el LLM.
**Solución futura:** Agregar el texto del TDR general al chunk de ítem (contextualización). Requiere re-chunking.

### Sin PDFs
**Situación:** Las contrataciones menores del SEACE no exponen PDFs vía API. El texto TDR completo viene de `desObjetoContrato` (en el detalle) y de los ítems CUBSO.
**Schema preparado:** Las columnas `pdf_texto` y `pdf_procesado` NO existen aún. Si en el futuro los PDFs fueran accesibles (para convocatorias mayores), el schema soportaría agregarlas.

### Rate limits de Workers AI Free
- **LLM (Llama 3.3 70B):** ~10,000 tokens de salida/día en free tier. Con ~500 tokens por respuesta, eso son ~20 queries RAG/día antes de llegar al límite.
- **Embeddings (BGE):** Sin límite documentado en tokens, pero con throttling. El pipeline de 9,077 embeddings tarda ~15 minutos.

### Dashboard: KPIs accionables
El dashboard actual muestra totales y gráficas, pero podría incluir:
- Alertas de contratos vigentes que cierran en < 3 días
- Contratos IT nuevos publicados hoy
- Gráfica de categorías IT a lo largo del tiempo

### Nota sobre el DOCUMENTACION.md
El archivo `DOCUMENTACION.md` en el repo describe la arquitectura **anterior** del Worker (recibía contratos del frontend, no hacía búsqueda vectorial). El estado actual del Worker (`src/index.ts`) implementa RAG completo: embeddings propios + pgvector + FTS fallback. El documento presente (PROYECTO_SEACE_MONITOR.md) refleja el estado real actual.

---

## 11. Cómo operar el sistema

### Operación diaria (automática)
El pipeline corre solo a las 06:00 AM Perú. No hay que hacer nada.

### Forzar una actualización manual
En GitHub → rdiazg14/seace-monitor → Actions → "Scrape SEACE contrataciones menores" → "Run workflow" → main → "Run workflow".

### Cambiar el threshold de similitud
```toml
# seace-ai-proxy/wrangler.toml
[vars]
SIMILARITY_THRESHOLD = "0.65"  # más resultados, más ruido
# SIMILARITY_THRESHOLD = "0.75"  # menos resultados, más precisión
```
Luego: `npx wrangler deploy` desde `seace-ai-proxy/`.

### Agregar una categoría IT nueva al clasificador
```python
# ingesta_completa.py — lista IT_CATS
IT_CATS: list[tuple[str, list[str]]] = [
    ...
    ("Nueva categoría", ["keyword1", "keyword2"]),  # agregar aquí
]
```
La nueva categoría aplica en la próxima corrida incremental. Los contratos ya en Supabase sin esa categoría no se re-clasifican automáticamente (habría que forzar `--forzar-completa`).

### Modificar el frontend
Push a `main` de `rdiazg14/seace-web` → deploy automático en ~2 min.

### Queries directas a Supabase
Supabase Dashboard → Project `wusywwhcyqngnpvpzxyr` → SQL Editor. O via REST API con la anon key.

### Redesplegar el Worker
```bash
cd seace-ai-proxy
npx wrangler deploy
```

### Actualizar credentials de Supabase en el pipeline
GitHub → rdiazg14/seace-monitor → Settings → Secrets and variables → Actions:
- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY`

**Importante:** Usar la pestaña "Legacy anon, service_role API keys" en Supabase Settings → API. La nueva UI "Publishable and secret API keys" genera tokens incompatibles con supabase-py.

---

## 12. Historial de decisiones técnicas

**Por qué Supabase y no otro backend**
pgvector está integrado nativamente (sin configuración extra), el free tier tiene PostgreSQL completo con RLS y vistas, y la anon key con RLS permite exponer la API directamente al frontend sin un backend propio.

**Por qué GitHub Pages y no Cloudflare Pages**
Ya se usaba Cloudflare Workers para el proxy — dos plataformas Cloudflare eran innecesarias. GitHub Pages tiene deploy automático integrado con Actions y CNAME/SSL triviales.

**Por qué Cloudflare Workers para el proxy RAG**
La Workers AI API key de Cloudflare no puede estar en el frontend (es secreta). El Worker actúa como proxy seguro, maneja CORS, y tiene acceso nativo al binding `AI` sin configuración extra.

**Por qué BGE-base-en-v1.5 y no otro modelo de embeddings**
Es el único modelo de embeddings gratuito disponible en Cloudflare Workers AI. No hay alternativa multilingüe en free tier al momento de la implementación.

**Por qué búsqueda híbrida vector + FTS**
BGE en inglés produce similaridades bajas para texto en español. Solo con vector, muchas queries devuelven 0-2 chunks. El fallback FTS (usando `ts_rank` en español) compensa el problema sin necesidad de re-entrenar el modelo.

**Por qué no hay PDFs**
El SEACE no expone PDFs vía API para contrataciones menores. El texto TDR viene de campos JSON (`desObjetoContrato`, ítems CUBSO). Si en el futuro hubiera PDFs, el schema ya tiene las columnas previstas.

**Por qué threshold 0.70**
Con 0.80 (el default del código), muchas queries válidas en español devolvían 0 chunks (BGE no es multilingüe). Se bajó a 0.70 vía env var en wrangler.toml. 0.70 incluye más falsos positivos pero con el LLM de 70B el contexto irrelevante se filtra naturalmente.

**Por qué SSE (Server-Sent Events)**
Sin SSE la latencia percibida era 14-15 segundos de pantalla en blanco. Con SSE el primer token aparece en ~0.5 segundos. La experiencia de usuario mejora radicalmente con el mismo tiempo de procesamiento total.

**Por qué Playwright y no requests directos**
La API del SEACE usa cookies de sesión SPA que solo se pueden obtener navegando la SPA real. `requests` o `httpx` no pueden reproducir ese flujo. Playwright lanza Chromium headless, navega la SPA, y luego reutiliza esas cookies con `page.request.get()`.

**Por qué ingesta incremental por MAX(id)**
Los contratos en la API están ordenados por `idContrato` descendente (orden=2). Los IDs son monotónicamente crecientes. Guardando el MAX(id) en Supabase, la ingesta incremental solo descarga la primera página hasta encontrar el primer registro ya conocido, completando en segundos en lugar de minutos.

---

*Documento generado el 2026-08-15. Datos validados con queries reales a Supabase y llamadas reales al Worker RAG.*
