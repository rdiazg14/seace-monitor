# SEACE Monitor — Documentación Técnica y Funcional

> Plataforma de monitoreo automático de contrataciones públicas del Estado peruano (SEACE).
> Versión de producción activa en **https://seace.rdiaz-lab.xyz**
> Última actualización: 2026-08-15

---

## Índice

1. [Qué es este proyecto](#1-qué-es-este-proyecto)
2. [Arquitectura general](#2-arquitectura-general)
3. [Versiones y plataformas](#3-versiones-y-plataformas)
4. [Repositorios](#4-repositorios)
5. [Capa 1 — Fuente de datos (SEACE API)](#5-capa-1--fuente-de-datos-seace-api)
6. [Capa 2 — Scraper y pipeline (seace-monitor)](#6-capa-2--scraper-y-pipeline-seace-monitor)
7. [Capa 3 — Base de datos (Supabase)](#7-capa-3--base-de-datos-supabase)
8. [Capa 4 — Frontend web (seace-web)](#8-capa-4--frontend-web-seace-web)
9. [Capa 5 — Proxy de IA (Cloudflare Worker)](#9-capa-5--proxy-de-ia-cloudflare-worker)
10. [Variables de entorno y secretos](#10-variables-de-entorno-y-secretos)
11. [Setup local](#11-setup-local)
12. [Despliegue en producción](#12-despliegue-en-producción)
13. [API pública — referencia](#13-api-pública--referencia)
14. [Integración con Claude y ChatGPT](#14-integración-con-claude-y-chatgpt)
15. [Problemas conocidos y soluciones](#15-problemas-conocidos-y-soluciones)

---

## 1. Qué es este proyecto

### Propósito

El **SEACE** (Sistema Electrónico de Contrataciones del Estado) es la plataforma oficial del Perú donde todas las entidades públicas publican sus procesos de contratación. SEACE Monitor es una plataforma que:

- **Descarga automáticamente** los 76,250 contratos publicados en 2026
- **Clasifica** cada contrato en categorías de tecnología (ciberseguridad, IA/analytics, Microsoft, Oracle, etc.)
- **Expone los datos** a través de una interfaz web con dashboard, buscador y chat con IA
- **Permite a cualquier desarrollador** consumir los datos vía REST API sin registrarse

### Para quién

Proveedores de tecnología que quieren identificar oportunidades en el sector público peruano: contratos vigentes, próximos a cerrar, clasificados por categoría IT.

### Flujo funcional completo

```
1. seace.gob.pe publica contratos (SPA pública)
         ↓
2. GitHub Actions corre a las 6:00 AM Perú (11:00 UTC)
         ↓
3. Python scraper navega la SPA con Playwright, extrae datos via API JSON interna
         ↓
4. Los datos se clasifican por categoría IT y relevancia IA (13 categorías, 3 niveles)
         ↓
5. Se hace UPSERT a Supabase (76,250 registros, batch de 500)
         ↓
6. La web React consulta Supabase en tiempo real (anon key pública)
         ↓
7. El usuario hace preguntas en lenguaje natural → frontend llama al CF Worker
         ↓
8. CF Worker usa Llama 3.3 70B (Workers AI) para analizar contratos y responder en español
```

---

## 2. Arquitectura general

```
┌─────────────────────────────────────────────────────────────────────┐
│                        FUENTES EXTERNAS                             │
│   seace.gob.pe — SPA pública sin autenticación — JSON paginado     │
└────────────────────────────┬────────────────────────────────────────┘
                             │ Playwright (browser headless)
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   CAPA 2: SCRAPER & PIPELINE                        │
│                   rdiazg14/seace-monitor                            │
│                                                                     │
│  ┌─────────────────────┐   ┌───────────────────────────────────┐   │
│  │ scraper_seace_       │   │ ingesta_completa.py               │   │
│  │ menores.py           │   │ - Modo COMPLETA (primera vez)     │   │
│  │ (scraper filtrado    │   │ - Modo INCREMENTAL (diario)       │   │
│  │  por keyword)        │   │ - Clasificación IT + IA           │   │
│  └─────────────────────┘   │ - UPSERT batch 500 a Supabase     │   │
│                             └───────────────────────────────────┘   │
│  GitHub Actions cron: 0 11 * * *  (11:00 UTC = 06:00 Perú)         │
└────────────────────────────┬────────────────────────────────────────┘
                             │ UPSERT via supabase-py (service_role)
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   CAPA 3: BASE DE DATOS                             │
│                   Supabase (PostgreSQL)                             │
│                   wusywwhcyqngnpvpzxyr.supabase.co                 │
│                                                                     │
│  tabla: contratos (76,250 filas, 14 columnas)                      │
│  views: dashboard_resumen | vigentes_urgentes                       │
│  rpc:   buscar_contratos() — FTS + filtros + paginación            │
│  RLS: anon → SELECT only | service_role → full access              │
└────────────────────────────┬────────────────────────────────────────┘
               ┌─────────────┘
               │ anon key (público, read-only)
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   CAPA 4: FRONTEND WEB                              │
│                   rdiazg14/seace-web                                │
│                   seace.rdiaz-lab.xyz (GitHub Pages)               │
│                                                                     │
│  /            Dashboard   — stats, gráficas, tabs                  │
│  /buscar      Buscador    — FTS, filtros, paginación               │
│  /chat        Chat IA     — lenguaje natural + análisis LLM        │
│  /docs        API Docs    — referencia + prompts para IA           │
│                                    │                               │
│                                    │ POST (query + contratos[])    │
│                                    ▼                               │
│                   ┌────────────────────────────┐                   │
│                   │   CAPA 5: AI PROXY         │                   │
│                   │   Cloudflare Worker        │                   │
│                   │   Llama 3.3 70B (fp8 fast) │                   │
│                   └────────────────────────────┘                   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Versiones y plataformas

### Backend / Scraper (seace-monitor)

| Componente | Versión |
|---|---|
| Python | 3.12 |
| uv (gestor de paquetes) | última estable |
| playwright | 1.62.0 |
| playwright browser | Chromium (headless) |
| pandas | 3.0.5 |
| pyarrow | 25.0.1 |
| supabase-py | 2.31.0 |
| GitHub Actions runner | ubuntu-latest |

### Frontend (seace-web)

| Componente | Versión |
|---|---|
| Node.js | v22.22.3 |
| npm | 10.9.8 |
| Vite | ^8.2.0 |
| React | ^19.2.8 |
| React DOM | ^19.2.8 |
| react-router-dom | ^7.18.2 |
| Recharts | ^3.10.1 |
| @supabase/supabase-js | ^2.112.3 |
| Tailwind CSS | ^3.4.19 |
| TypeScript | ~6.0.2 |
| @vitejs/plugin-react | ^6.0.4 |
| postcss | ^8.5.26 |
| autoprefixer | ^10.5.4 |

### Infraestructura

| Componente | Plataforma | Detalles |
|---|---|---|
| Base de datos | Supabase Free tier | PostgreSQL 15, región sa-east-1 (São Paulo) |
| Hosting frontend | GitHub Pages | Build type: workflow, HTTPS automático |
| Domain | Namecheap | CNAME `seace → rdiazg14.github.io` |
| AI proxy | Cloudflare Workers | Wrangler 4.123.0 |
| Modelo LLM | Cloudflare Workers AI | `@cf/meta/llama-3.3-70b-instruct-fp8-fast` |
| CI/CD scraper | GitHub Actions | ubuntu-latest, timeout 30 min |
| CI/CD frontend | GitHub Actions | ubuntu-latest, Node 22 |

### Sistema operativo de desarrollo

Windows 11 Pro (10.0.26200), PowerShell como shell primario.

---

## 4. Repositorios

| Repo | URL GitHub | Descripción |
|---|---|---|
| seace-monitor | `rdiazg14/seace-monitor` | Scraper Python + GitHub Actions + datos |
| seace-web | `rdiazg14/seace-web` | Frontend React + CI/CD |
| seace-ai-proxy | Solo en Cloudflare (sin git) | Worker de IA |

### Estructura de seace-monitor

```
seace-monitor/
├── .env                          # Secretos locales (gitignored)
├── .gitignore
├── ingesta_completa.py           # Ingesta corpus completo/incremental → Supabase
├── scraper_seace_menores.py      # Scraper filtrado por keyword
├── data/
│   ├── seace_menores_completo.parquet   # Backup local (snappy)
│   ├── seace_menores_completo.csv       # CSV (últimas 1000 filas si > 100 MB)
│   ├── contratos_token.csv             # Salida del scraper filtrado
│   ├── ultima_ingesta.txt              # Timestamp + stats de la última corrida
│   └── ultima_corrida.txt              # Timestamp de la última ejecución GH Actions
└── .github/
    └── workflows/
        └── scrape.yml            # Automatización GitHub Actions
```

### Estructura de seace-web

```
seace-web/
├── public/
│   └── CNAME                    # seace.rdiaz-lab.xyz (custom domain GitHub Pages)
├── src/
│   ├── main.tsx                 # Entry point
│   ├── App.tsx                  # Router + layout raíz
│   ├── types.ts                 # Interfaces TypeScript (Contrato, DashboardResumen)
│   ├── index.css                # Global styles + Tailwind directives
│   ├── App.css                  # Estilos específicos de la app
│   ├── lib/
│   │   └── supabase.ts          # createClient con anon key
│   ├── components/
│   │   ├── Navbar.tsx           # Navegación sticky con NavLink activos
│   │   └── ContratoCard.tsx     # Tarjeta de contrato reutilizable
│   ├── pages/
│   │   ├── Dashboard.tsx        # Página principal con stats y gráficas
│   │   ├── Buscador.tsx         # Búsqueda full-text con filtros
│   │   ├── Chat.tsx             # Chat con IA (lenguaje natural)
│   │   └── Docs.tsx             # Documentación de la API pública
│   └── assets/
│       └── hero.png
├── vite.config.ts               # base: '/' (custom domain)
├── tailwind.config.js
├── tsconfig.json
├── package.json
└── .github/
    └── workflows/
        └── deploy.yml           # Build + deploy a GitHub Pages en cada push a main
```

### Estructura de seace-ai-proxy

```
seace-ai-proxy/
├── src/
│   └── index.ts                 # Worker TypeScript
├── wrangler.toml                # Config Cloudflare (nombre, AI binding, account_id)
└── package.json                 # devDependencies: wrangler, @cloudflare/workers-types
```

---

## 5. Capa 1 — Fuente de datos (SEACE API)

### Funcional

El SEACE expone una SPA (Single Page Application) pública en `https://prod6.seace.gob.pe/buscador-publico/contrataciones`. Esta SPA consume internamente una API JSON REST. No requiere autenticación ni registro.

Los datos cubren **contrataciones menores** del año en curso (2026): 76,250 procesos de contratación de todas las entidades del Estado peruano.

### Técnico

| Parámetro | Valor |
|---|---|
| SPA URL | `https://prod6.seace.gob.pe/buscador-publico/contrataciones` |
| API JSON | `https://prod6.seace.gob.pe/v1/s8uit-services/buscadorpublico/contrataciones/buscador` |
| Método | GET |
| Auth | Ninguna |
| Paginación | `page` (1-based) + `page_size` (máx 100) |

**Parámetros de la API:**

```
anio        = 2026        (año actual; requerido pero no filtra efectivamente)
palabra_clave = ""         (vacío para corpus completo)
orden       = 2            (ordenado por fecha desc)
page        = 1 … 763
page_size   = 100
```

**Estructura de la respuesta:**

```json
{
  "pageable": {
    "totalElements": 76250,
    "totalPages": 763
  },
  "data": [
    {
      "idContrato":         12345,
      "nroContratacion":    "CM-001-2026-MINSA",
      "desContratacion":    "ADQUISICION DE TOKEN CRIPTOGRAFICO...",
      "desObjetoContrato":  "Bien",
      "nomObjetoContrato":  "Bien",
      "nomEntidad":         "MINISTERIO DE SALUD",
      "nomEstadoContrato":  "Vigente",
      "fecPublica":         "01/08/2026 00:00:00",
      "fecIniCotizacion":   "02/08/2026 08:00:00",
      "fecFinCotizacion":   "10/08/2026 17:00:00",
      "idTipoCotizacion":   1,
      "cotizar":            true
    }
  ]
}
```

**Por qué se usa Playwright:**  
La API está protegida por cookies de sesión que solo se obtienen navegando la SPA con un browser real. Playwright inicia Chromium en modo headless, navega la SPA, y luego usa `page.request.get()` para llamar directamente a la API aprovechando esas cookies.

---

## 6. Capa 2 — Scraper y pipeline (seace-monitor)

### 6.1 scraper_seace_menores.py

**Propósito funcional:** Scraper rápido filtrado por keyword y objeto. Genera un CSV por búsqueda específica. Úsalo cuando quieres resultados de una búsqueda particular (ej: "token + Servicio").

**Uso:**

```bash
uv run python scraper_seace_menores.py \
  --keyword token \
  --objeto Servicio \
  --out data/contratos_token.csv
```

**Parámetros:**

| Parámetro | Descripción |
|---|---|
| `--keyword` | Texto a buscar (puede ir vacío para todos) |
| `--objeto` | `Bien` \| `Servicio` \| `Obra` \| `"Consultoria de Obra"` |
| `--entidad` | Filtro por nombre de entidad |
| `--out` | Archivo de salida CSV |
| `--anio` | Año (default: año actual) |
| `--headed` | Abre el browser visible (útil para depurar) |

**Clasificación de relevancia IA:**

```python
KW_ALTA     = ["token", "azure openai", "openai", "gpt", "llm",
               "claude", "copilot", "gemini"]

KW_GENERICOS = ["inteligencia artificial", "ia generativa", "chatbot",
                "asistente virtual", "machine learning", "aprendizaje automatico",
                "procesamiento de lenguaje", "vision computacional",
                "deep learning", "red neuronal", "modelo de lenguaje",
                "ciencia de datos", "big data"]

# ALTA: cualquier keyword de KW_ALTA aparece en la descripción
# MEDIA: 2+ keywords de KW_GENERICOS
# BAJA: 1 keyword de KW_GENERICOS
```

Todos los keywords se normalizan (minúsculas + sin tildes) antes de comparar.

---

### 6.2 ingesta_completa.py

**Propósito funcional:** Ingesta el corpus completo de 76,250 contratos a Supabase. Tiene dos modos:

- **COMPLETA:** Primera corrida o con `--forzar-completa`. Descarga las 763 páginas completas.
- **INCREMENTAL:** Corridas diarias. Detecta el `MAX(id)` en Supabase y solo descarga registros nuevos. Se detiene cuando encuentra el primer registro ya conocido.

**Clasificación IT por reglas (13 categorías):**

| Categoría | Keywords principales |
|---|---|
| Firma digital | firma digital, certificado digital, token criptografico |
| IA/analytics | inteligencia artificial, machine learning, gpt, llm, claude, copilot, gemini |
| Ciberseguridad | ciberseguridad, seguridad informatica, firewall, pentest |
| Cloud/hosting | nube publica, cloud computing, hosting, aws, google cloud |
| Microsoft | microsoft, office 365, sharepoint, exchange, windows server |
| Oracle | oracle database, oracle ebs, peoplesoft |
| Base de datos/ERP | base de datos, sql server, postgresql, mysql, sap, erp |
| Desarrollo software | desarrollo de software, sistema de informacion, plataforma web, app movil |
| Licencias | licencia de software, licenciamiento, suscripcion de software |
| Soporte tecnico | soporte tecnico, mantenimiento de software, helpdesk |
| Redes/cableado | red de datos, cableado estructurado, switch, fibra optica, wifi |
| Correo electronico | correo electronico, mensajeria electronica |
| Hardware | computadora, laptop, impresora, monitor, disco duro, ups, tablet |

**Algoritmo de clasificación:** Normalización NFKD (sin tildes, minúsculas) + búsqueda de substring. Para keywords de una sola palabra (`ia`, `switch`) se aplica `\b` (límite de palabra). Primera categoría que coincide gana (orden = prioridad).

**UPSERT a Supabase:**
- Batch de 500 registros por lote
- Conflicto en columna `id` → actualiza
- Retry exponencial hasta 3 intentos por lote (espera 2s, 4s)
- Ingesta completa de 76,250 registros: ~60 segundos

**Backup local:**
- `data/seace_menores_completo.parquet` — comprimido snappy, lectura rápida
- `data/seace_menores_completo.csv` — si supera 100 MB, guarda solo las últimas 1,000 filas
- `data/ultima_ingesta.txt` — timestamp + total_registros + nuevos_esta_corrida

---

### 6.3 GitHub Actions — scrape.yml

**Schedule:** `0 11 * * *` = 11:00 UTC = **06:00 hora Perú** (UTC−5), todos los días.
También disponible como `workflow_dispatch` (ejecución manual desde GitHub).

**Configuración:**
- Runner: `ubuntu-latest`
- Timeout: 30 minutos
- Permisos: `contents: write` (para hacer auto-commit de `data/`)
- Concurrencia: grupo `scrape-seace`, sin cancelar en progreso

**Pasos de ejecución:**

```
1. Checkout del repositorio
2. Setup Python 3.12
3. pip install playwright pandas pyarrow supabase
4. playwright install --with-deps chromium
5. python scraper_seace_menores.py --keyword token --objeto Servicio --out data/contratos_token.csv
6. python ingesta_completa.py   ← con SUPABASE_URL y SUPABASE_SERVICE_KEY como env vars
7. date -u > data/ultima_corrida.txt
8. git add data/ → commit si hay cambios → git push
```

**Commit automático:** Si `data/` cambia (hay contratos nuevos), el bot de GitHub Actions commitea con el mensaje:
```
chore(data): SEACE 2026-08-15 — corpus=76,250 nuevos=0
```

---

## 7. Capa 3 — Base de datos (Supabase)

### Configuración del proyecto

| Campo | Valor |
|---|---|
| Proyecto ID | `wusywwhcyqngnpvpzxyr` |
| URL | `https://wusywwhcyqngnpvpzxyr.supabase.co` |
| Región | sa-east-1 (São Paulo) |
| Plan | Free tier |
| Motor | PostgreSQL 15 |

**Dónde obtener las API keys:**  
Supabase → Settings → API → pestaña **"Legacy anon, service_role API keys"**.  
⚠️ No usar la nueva pestaña "Publishable and secret API keys" — el cliente supabase-py requiere los tokens JWT de la interfaz legacy.

---

### Tabla principal: `contratos`

| Columna | Tipo | Descripción |
|---|---|---|
| `id` | `int8` (PK) | `idContrato` de la API SEACE. Clave de UPSERT. |
| `nro_contratacion` | `text` | Número del proceso (ej: CM-001-2026-MINSA) |
| `descripcion_contrato` | `text` ⁽¹⁾ | Título del proceso de contratación |
| `objeto` | `text` | Bien · Servicio · Obra · Consultoría de Obra |
| `descripcion` | `text` ⁽¹⁾ | Descripción del objeto contratado |
| `entidad` | `text` ⁽¹⁾ | Nombre de la entidad del Estado peruano |
| `estado` | `text` | Vigente · En Evaluación · Culminado · Cancelado |
| `fecha_publica` | `timestamptz` | Fecha de publicación (convertida de dd/mm/yyyy) |
| `fecha_ini_cotizacion` | `timestamptz` | Inicio del período de cotización |
| `fecha_fin_cotizacion` | `timestamptz` | Cierre de cotización (indica urgencia) |
| `tipo_cotizacion` | `text` | ID del tipo de cotización |
| `cotizar` | `bool` | Si acepta cotizaciones |
| `categoria_it` | `text \| null` ⁽²⁾ | Categoría IT (13 posibles) asignada por el scraper |
| `relevancia_ia` | `text \| null` ⁽²⁾ | ALTA · MEDIA · BAJA — relevancia para IA generativa |

⁽¹⁾ Incluido en el índice FTS (búsqueda de texto completo en español).  
⁽²⁾ Columnas añadidas por el pipeline; no vienen de la API SEACE.

**Índice FTS:**
```sql
CREATE INDEX contratos_fts_idx ON contratos
USING gin(
  to_tsvector('spanish',
    coalesce(descripcion_contrato, '') || ' ' ||
    coalesce(descripcion, '') || ' ' ||
    coalesce(entidad, '')
  )
);
```

---

### Row Level Security (RLS)

```sql
-- Política para el rol anon (clave pública, usada en el frontend)
CREATE POLICY "anon puede select" ON contratos
  FOR SELECT
  TO anon
  USING (true);

-- service_role tiene acceso completo sin políticas adicionales
```

La clave `anon` permite SELECT en todas las filas. No permite INSERT, UPDATE ni DELETE. La clave `service_role` tiene acceso completo y solo se usa en el scraper backend.

---

### Vistas

**`dashboard_resumen`**
```sql
SELECT
  objeto,
  estado,
  categoria_it,
  DATE_TRUNC('month', fecha_publica) AS mes,
  COUNT(*) AS total
FROM contratos
GROUP BY 1, 2, 3, 4;
```
Usada por la página Dashboard para calcular las 4 stat cards, el BarChart por mes y el PieChart por objeto.

**`vigentes_urgentes`**
```sql
SELECT *
FROM contratos
WHERE estado = 'Vigente'
ORDER BY fecha_fin_cotizacion ASC;
```
Contratos con cotización abierta, ordenados por cierre más próximo. Usada en el tab "Vigentes" del Dashboard.

---

### Función RPC: `buscar_contratos()`

```sql
CREATE OR REPLACE FUNCTION buscar_contratos(
  termino       text DEFAULT '',
  filtro_objeto  text DEFAULT NULL,
  filtro_estado  text DEFAULT NULL,
  filtro_entidad text DEFAULT NULL,
  limite        int  DEFAULT 20,
  offset_val    int  DEFAULT 0
)
RETURNS SETOF contratos
LANGUAGE sql STABLE
AS $$
  SELECT *
  FROM contratos
  WHERE
    (termino = '' OR
     to_tsvector('spanish', coalesce(descripcion_contrato,'') || ' ' ||
                             coalesce(descripcion,'') || ' ' ||
                             coalesce(entidad,''))
     @@ plainto_tsquery('spanish', termino))
    AND (filtro_objeto  IS NULL OR objeto  = filtro_objeto)
    AND (filtro_estado  IS NULL OR estado  = filtro_estado)
    AND (filtro_entidad IS NULL OR entidad ILIKE '%' || filtro_entidad || '%')
  ORDER BY
    CASE WHEN termino = '' THEN 0
         ELSE -ts_rank(
           to_tsvector('spanish', coalesce(descripcion_contrato,'') || ' ' ||
                                   coalesce(descripcion,'') || ' ' ||
                                   coalesce(entidad,'')),
           plainto_tsquery('spanish', termino)
         )
    END,
    fecha_publica DESC
  LIMIT limite
  OFFSET offset_val;
$$;
```

---

## 8. Capa 4 — Frontend web (seace-web)

### Funcional

La web tiene 4 páginas accesibles desde la barra de navegación superior:

| Ruta | Nombre | Propósito |
|---|---|---|
| `/` | Dashboard | Vista general con estadísticas, gráficas y listados |
| `/buscar` | Buscador | Búsqueda full-text con filtros y paginación |
| `/chat` | Chat | Consultas en lenguaje natural con análisis de IA |
| `/docs` | API | Documentación de la API pública |

---

### Técnico — stack

El frontend es una SPA estática compilada con Vite y desplegada en GitHub Pages.

**Routing:** `react-router-dom` v7. `BrowserRouter` en la raíz. `vite.config.ts` usa `base: '/'` porque el sitio tiene dominio custom (si fuera subpath usaría el nombre del repo).

**Cliente Supabase:** La clave `anon` está hardcoded en `src/lib/supabase.ts`. Es seguro porque la clave solo permite SELECT con RLS.

```typescript
// src/lib/supabase.ts
import { createClient } from '@supabase/supabase-js'

const SUPABASE_URL = 'https://wusywwhcyqngnpvpzxyr.supabase.co'
const SUPABASE_ANON_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...'

export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY)
```

**Interfaces TypeScript (`src/types.ts`):**

```typescript
export interface Contrato {
  id: number
  nro_contratacion: string
  descripcion_contrato: string
  objeto: string
  descripcion: string
  entidad: string
  estado: string
  fecha_publica: string | null
  fecha_ini_cotizacion: string | null
  fecha_fin_cotizacion: string | null
  tipo_cotizacion: string | null
  cotizar: boolean | null
  categoria_it: string | null
  relevancia_ia: string | null
  rank?: number
}

export interface DashboardResumen {
  objeto: string
  estado: string
  categoria_it: string | null
  mes: string
  total: number
}
```

---

### Página: Dashboard (`/`)

**Datos cargados en paralelo al montar el componente:**

```typescript
const [res1, res2, res3] = await Promise.all([
  supabase.from('dashboard_resumen').select('*'),
  supabase.from('vigentes_urgentes').select('*').limit(30),
  supabase.from('contratos')
    .select('*')
    .not('categoria_it', 'is', null)
    .order('fecha_publica', { ascending: false })
    .limit(50),
])
```

**Componentes UI:**
- **4 stat cards:** Total contratos 2026 | Contratos Vigentes | Contratos IT | IA/analytics
- **BarChart (Recharts):** Contratos por mes en 2026, agrupados de `dashboard_resumen`
- **PieChart (Recharts):** Distribución por objeto (Bien/Servicio/Obra/Consultoría)
- **Tab "Resumen":** Las gráficas + stats
- **Tab "Vigentes":** Lista de `vigentes_urgentes` renderizada con `ContratoCard`
- **Tab "IT":** Contratos con `categoria_it` renderizados con `ContratoCard`

---

### Página: Buscador (`/buscar`)

**Flujo:**
1. Usuario escribe texto y/o selecciona filtros (objeto, estado)
2. `handleSearch()` llama `supabase.rpc('buscar_contratos', params)`
3. Resultados renderizados como `ContratoCard` con paginación de 20 por página

**Filtros disponibles:**
- Texto libre (full-text search en español)
- Objeto: Bien · Servicio · Obra · Consultoría de Obra
- Estado: Vigente · En Evaluación · Culminado · Cancelado

---

### Página: Chat (`/chat`)

**Flujo completo por mensaje enviado:**

```
1. parsearConsulta(input)
   → extrae termino, filtro_objeto, filtro_estado mediante regex en español
   
2. supabase.rpc('buscar_contratos', { termino, filtro_objeto, filtro_estado, limite: 6 })
   → recupera hasta 6 contratos relevantes
   
3. fetch(AI_PROXY, { method: 'POST', body: { query, contratos } })
   → el Worker llama a Llama 3.3 70B con el contexto
   
4. Muestra respuesta IA + tarjetas de contratos (ContratoCard) bajo el mensaje

✗ Si la IA falla: fallback a texto simple con conteo
```

**Sugerencias rápidas** (solo se muestran cuando hay 1 solo mensaje — el de bienvenida):
- "contratos de token vigentes"
- "ciberseguridad servicio"
- "inteligencia artificial vigente"
- "oracle base de datos"
- "microsoft 365"
- "desarrollo de software vigente"

---

### Componente: ContratoCard

Renderiza un contrato con:
- Badge de estado con color semántico: Vigente (verde) · En Evaluación (ámbar) · otros (gris)
- Badge de objeto
- Badge de relevancia IA (si existe): rojo
- Descripción del contrato (truncada a 2 líneas)
- Nombre de la entidad
- Fecha de cierre de cotización (si existe)

---

### Deploy (GitHub Actions — deploy.yml)

```yaml
on:
  push:
    branches: [main]
  workflow_dispatch: {}

steps:
  - Checkout
  - Setup Node 22 + cache npm
  - npm ci
  - npm run build    # tsc -b && vite build → dist/
  - configure-pages
  - upload-pages-artifact (path: dist)
  - deploy-pages
```

**Duración total:** ~2 minutos desde el push hasta que el cambio está live.

**DNS:** CNAME `seace → rdiazg14.github.io` configurado en Namecheap. SSL automático vía GitHub Pages (Let's Encrypt).

---

## 9. Capa 5 — Proxy de IA (Cloudflare Worker)

### Funcional

El Worker actúa como intermediario entre el frontend y el modelo de lenguaje. El frontend no puede llamar directamente a Workers AI porque:
1. Requiere credenciales de Cloudflare (secretas)
2. Necesita configurar CORS
3. Construye el prompt del sistema + el contexto de contratos

El Worker recibe la consulta del usuario y los contratos encontrados, construye un prompt estructurado, llama al modelo Llama 3.3 70B y devuelve la respuesta en español. Siempre devuelve HTTP 200 (nunca rompe el chat del usuario).

### Técnico

**Plataforma:** Cloudflare Workers (Edge Computing)  
**URL:** `https://seace-ai-proxy.rdiazg14.workers.dev`  
**Account ID:** `5a2b884f36bd62011960b879c3737546`  
**Wrangler:** 4.123.0

**wrangler.toml:**
```toml
name = "seace-ai-proxy"
main = "src/index.ts"
compatibility_date = "2024-11-01"
account_id = "5a2b884f36bd62011960b879c3737546"

[ai]
binding = "AI"   # Disponible en el worker como env.AI
```

**Modelo:** `@cf/meta/llama-3.3-70b-instruct-fp8-fast`  
(El modelo anterior `@cf/meta/llama-3.1-8b-instruct` fue deprecado el 2026-05-30 por Cloudflare.)

**Interface HTTP:**

```typescript
// Request
POST https://seace-ai-proxy.rdiazg14.workers.dev
Content-Type: application/json
Origin: https://seace.rdiaz-lab.xyz

{
  "query": "token criptografico vigente",
  "contratos": [
    {
      "id": 12345,
      "descripcion": "ADQUISICION DE TOKEN CRIPTOGRAFICO",
      "entidad": "MINISTERIO DE SALUD",
      "estado": "Vigente",
      "objeto": "Bien",
      "relevancia_ia": "ALTA",
      "categoria_it": "Firma digital",
      "fecha_fin_cotizacion": "2026-08-20T17:00:00+00:00"
    }
  ]
}

// Response — siempre HTTP 200
{ "response": "Al analizar el contrato..." }

// Response de error (también HTTP 200)
{ "error": "msg del error", "response": "Encontré N contratos para X." }
```

**CORS — orígenes permitidos:**
```typescript
const ORIGINS = [
  'https://seace.rdiaz-lab.xyz',
  'https://rdiazg14.github.io',
  'http://localhost:5173',
]
```

**Prompts del sistema:**

```
[system]
Eres un asistente experto en contrataciones públicas del Estado peruano (SEACE).
Responde siempre en español, de forma concisa (máximo 3 párrafos cortos).
Basa tu respuesta en los contratos proporcionados.

[user — con resultados]
Encontré estos contratos para la consulta "TOKEN":

1. ADQUISICION DE TOKEN CRIPTOGRAFICO [IA ALTA]
   Entidad: MINISTERIO DE SALUD · Vigente · Bien · cierre 20/08/2026

¿Puedes analizarlos y decirme qué destacas?

[user — sin resultados]
Busqué "XYZ" en la base de datos SEACE pero no encontré resultados.
¿Puedes sugerir términos alternativos?
```

**Lógica de manejo de errores:**

```typescript
try {
  const aiResponse = await env.AI.run('@cf/meta/llama-3.3-70b-instruct-fp8-fast', {
    messages: [...],
    max_tokens: 512,
  })
  return new Response(JSON.stringify({ response: aiResponse.response }), { status: 200 })
} catch (err) {
  // No rompe el chat del usuario
  return new Response(
    JSON.stringify({
      error: err.message,
      response: `Encontré ${contratos.length} contratos para "${query}".`
    }),
    { status: 200 }
  )
}
```

**Nota importante — tipado:** `env.AI` está tipado como `any` (no como `Ai` del paquete `@cloudflare/workers-types`). El tipado estricto causa un error de startup en el runtime de Cloudflare (error 1101). `any` resuelve el problema sin afectar funcionalidad.

---

## 10. Variables de entorno y secretos

### seace-monitor

| Variable | Valor | Dónde vive |
|---|---|---|
| `SUPABASE_URL` | `https://wusywwhcyqngnpvpzxyr.supabase.co` | `.env` local + GitHub Secret |
| `SUPABASE_SERVICE_KEY` | `eyJhbGci...` (JWT service_role) | `.env` local + GitHub Secret — **NUNCA en código** |

### seace-web (frontend)

| Variable | Valor | Dónde vive |
|---|---|---|
| `SUPABASE_ANON_KEY` | `eyJhbGci...` (JWT anon) | Hardcoded en `src/lib/supabase.ts` — es pública y segura |

### seace-ai-proxy (Cloudflare Worker)

No necesita variables de entorno adicionales. El binding `AI` de Workers AI se configura en `wrangler.toml` y Cloudflare lo inyecta automáticamente en `env.AI`.

### Archivo .env local (seace-monitor)

```env
SUPABASE_URL=https://wusywwhcyqngnpvpzxyr.supabase.co
SUPABASE_SERVICE_KEY=eyJhbGci...   ← obtener de Supabase Settings > API > Legacy
SUPABASE_ANON_KEY=eyJhbGci...     ← no requerida por el scraper, solo referencia
```

`.env` está en `.gitignore`. Nunca se commitea.

---

## 11. Setup local

### seace-monitor (scraper Python)

**Prerrequisitos:** Python 3.12, uv instalado.

```bash
# Clonar y entrar al directorio
git clone https://github.com/rdiazg14/seace-monitor.git
cd seace-monitor

# Crear entorno virtual e instalar dependencias
uv venv
uv pip install playwright pandas pyarrow supabase

# Instalar browser Chromium
uv run playwright install chromium

# Crear .env con las credenciales de Supabase
# (ver sección anterior)

# Ejecutar scraper filtrado (rápido, ~30s)
uv run python scraper_seace_menores.py --keyword token --objeto Servicio

# Ejecutar ingesta completa (primera vez: ~12 min; incremental: <2 min)
uv run python ingesta_completa.py

# Forzar descarga completa (ignora MAX(id) en Supabase)
uv run python ingesta_completa.py --forzar-completa

# Modo con browser visible (para depurar)
uv run python ingesta_completa.py --headed
```

### seace-web (frontend React)

**Prerrequisitos:** Node.js v22+.

```bash
git clone https://github.com/rdiazg14/seace-web.git
cd seace-web
npm install
npm run dev        # Dev server en http://localhost:5173
npm run build      # Build para producción → dist/
```

El frontend se conecta a Supabase usando la clave anon hardcoded — no necesita `.env`.

### seace-ai-proxy (Cloudflare Worker)

**Prerrequisitos:** Node.js v22+, cuenta Cloudflare con Workers AI habilitado.

```bash
cd seace-ai-proxy
npm install
npx wrangler login     # Autenticar con Cloudflare
npx wrangler dev       # Dev local (simula Workers AI)
npx wrangler deploy    # Despliega a producción
```

---

## 12. Despliegue en producción

### seace-monitor

El despliegue es automático vía GitHub Actions. No hay infraestructura de servidor — el scraper corre en runners efímeros de GitHub (ubuntu-latest).

Para agregar o rotar las credenciales de Supabase:
```
GitHub → rdiazg14/seace-monitor → Settings → Secrets and variables → Actions
→ SUPABASE_URL
→ SUPABASE_SERVICE_KEY
```

### seace-web

Cualquier push a la rama `main` dispara automáticamente el workflow `deploy.yml`:
1. Checkout + Node 22
2. `npm ci` (instala desde `package-lock.json`)
3. `npm run build` (`tsc -b && vite build`)
4. Sube `dist/` como artefacto de Pages
5. Despliega en GitHub Pages

El dominio custom `seace.rdiaz-lab.xyz` se configura en:
- **GitHub:** Settings → Pages → Custom domain → `seace.rdiaz-lab.xyz`
- **Namecheap DNS:** CNAME record `seace` → `rdiazg14.github.io` (TTL automático)
- **Archivo `public/CNAME`:** contiene `seace.rdiaz-lab.xyz` (incluido en el build)

### seace-ai-proxy

No hay CI/CD automático para el Worker. El deploy se hace manualmente:

```bash
cd seace-ai-proxy
npx wrangler deploy
```

Si se cambia el modelo de IA u otra configuración:
1. Editar `src/index.ts`
2. `npx wrangler deploy`
3. Verificar con curl (ver sección 13)

---

## 13. API pública — referencia

### Base URL y autenticación

```
Base URL: https://wusywwhcyqngnpvpzxyr.supabase.co
API Key:  eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Ind1c3l3d2hjeXFuZ25wdnB6eHlyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODY3NDc0NDcsImV4cCI6MjEwMjMyMzQ0N30.jDZeGaW8lQuROU7IF11clkfjgyyiMrgyIfi6LvuAFeY
```

Headers requeridos en todas las peticiones:
```
apikey: [API_KEY]
Authorization: Bearer [API_KEY]
Content-Type: application/json
```

### POST /rest/v1/rpc/buscar_contratos

Búsqueda full-text con filtros opcionales.

```bash
curl -X POST 'https://wusywwhcyqngnpvpzxyr.supabase.co/rest/v1/rpc/buscar_contratos' \
  -H "apikey: eyJhbGci..." \
  -H "Authorization: Bearer eyJhbGci..." \
  -H "Content-Type: application/json" \
  -d '{
    "termino": "ciberseguridad",
    "filtro_objeto": "Servicio",
    "filtro_estado": "Vigente",
    "filtro_entidad": null,
    "limite": 10,
    "offset_val": 0
  }'
```

### GET /rest/v1/dashboard_resumen

```bash
curl 'https://wusywwhcyqngnpvpzxyr.supabase.co/rest/v1/dashboard_resumen' \
  -H "apikey: eyJhbGci..." \
  -H "Authorization: Bearer eyJhbGci..."
```

### GET /rest/v1/vigentes_urgentes

```bash
curl 'https://wusywwhcyqngnpvpzxyr.supabase.co/rest/v1/vigentes_urgentes?limit=20' \
  -H "apikey: eyJhbGci..." \
  -H "Authorization: Bearer eyJhbGci..."
```

### POST https://seace-ai-proxy.rdiazg14.workers.dev

```bash
curl -X POST 'https://seace-ai-proxy.rdiazg14.workers.dev' \
  -H "Content-Type: application/json" \
  -H "Origin: https://seace.rdiaz-lab.xyz" \
  -d '{"query": "token vigente", "contratos": [...]}'
```

### Ejemplo en Python

```python
import requests

URL = 'https://wusywwhcyqngnpvpzxyr.supabase.co'
KEY = 'eyJhbGci...'

def buscar(termino, objeto=None, estado=None, limite=10):
    r = requests.post(
        f'{URL}/rest/v1/rpc/buscar_contratos',
        headers={'apikey': KEY, 'Authorization': f'Bearer {KEY}', 'Content-Type': 'application/json'},
        json={'termino': termino, 'filtro_objeto': objeto, 'filtro_estado': estado,
              'filtro_entidad': None, 'limite': limite, 'offset_val': 0}
    )
    r.raise_for_status()
    return r.json()

# Ejemplos
print(buscar('ciberseguridad', estado='Vigente'))
print(buscar('oracle', objeto='Servicio'))
print(buscar('inteligencia artificial'))
```

---

## 14. Integración con Claude y ChatGPT

### System prompt para Claude Projects

Pega este texto en las instrucciones del sistema de un Proyecto Claude:

```
Eres un asistente especializado en contrataciones públicas del Estado peruano (SEACE).
Tienes acceso a una base de datos con 76,250 contratos publicados en 2026.

## Endpoint de búsqueda

POST https://wusywwhcyqngnpvpzxyr.supabase.co/rest/v1/rpc/buscar_contratos
Headers:
  apikey: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Ind1c3l3d2hjeXFuZ25wdnB6eHlyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODY3NDc0NDcsImV4cCI6MjEwMjMyMzQ0N30.jDZeGaW8lQuROU7IF11clkfjgyyiMrgyIfi6LvuAFeY
  Authorization: Bearer [mismo valor]
  Content-Type: application/json

Body: { "termino": "texto", "filtro_objeto": null, "filtro_estado": null,
        "filtro_entidad": null, "limite": 10, "offset_val": 0 }

## Instrucciones

1. Llama al endpoint cuando el usuario pregunte sobre contratos SEACE.
2. Presenta: entidad, descripción, estado, fecha de cierre.
3. Prioriza contratos Vigentes con cierre próximo.
4. Responde siempre en español.
5. Si no hay resultados, sugiere términos alternativos.
```

### System prompt para ChatGPT (Custom Instructions / GPTs con Code Interpreter)

```
Eres un experto en el SEACE (contrataciones públicas de Perú).
Usa Code Interpreter para llamar a esta API:

POST https://wusywwhcyqngnpvpzxyr.supabase.co/rest/v1/rpc/buscar_contratos
API KEY: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Ind1c3l3d2hjeXFuZ25wdnB6eHlyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODY3NDc0NDcsImV4cCI6MjEwMjMyMzQ0N30.jDZeGaW8lQuROU7IF11clkfjgyyiMrgyIfi6LvuAFeY

Body: { termino, filtro_objeto, filtro_estado, filtro_entidad: null, limite: 10, offset_val: 0 }

Responde en español. Identifica contratos Vigentes con cotización abierta.
```

---

## 15. Problemas conocidos y soluciones

| Problema | Causa | Solución |
|---|---|---|
| Supabase muestra "Publishable and secret API keys" | Nueva interfaz de Supabase | Usar pestaña **"Legacy anon, service_role API keys"** que tiene los JWT compatibles con supabase-py |
| `git rebase --continue` falla en PowerShell con `--no-edit` | PowerShell no soporta esa flag | Usar `$env:GIT_EDITOR = "true"; git rebase --continue` |
| CF Worker error 1101 (unhandled exception al startup) | `env.AI: Ai` (tipo estricto de @cloudflare/workers-types) causa fallo en el runtime | Cambiar a `env.AI: any` |
| CF Worker error 5028 — modelo deprecado | `@cf/meta/llama-3.1-8b-instruct` deprecado el 2026-05-30 | Usar `@cf/meta/llama-3.3-70b-instruct-fp8-fast` |
| Recharts PieChart TypeScript error | `percent` y `name` del prop `label` son `undefined` posibles | Tipar explícitamente: `({ name, percent }: { name?: string; percent?: number })` y usar `?? 0` / `?? ''` |
| Python logs no aparecen mientras corre | stdout bufferizado cuando se redirige a archivo | Agregar `flush=True` a los prints, o ejecutar sin redirigir a archivo |
| GitHub Pages retorna 404 | GitHub Actions corrió antes de que Pages estuviera habilitado vía API | Habilitar Pages primero (Settings → Pages), luego disparar `workflow_dispatch` manualmente |
| Conflicto en git push (GitHub Actions bot commitó mientras trabajabas) | El bot de Actions commitó `data/` mientras el repo local tenía cambios sin push | `git pull --rebase origin main`, resolver conflicto en `data/ultima_ingesta.txt` (quedarse con la versión local), `$env:GIT_EDITOR = "true"; git rebase --continue`, `git push` |
| Ingesta parece colgarse (log vacío) | Python bufferiza stdout al escribir en archivo | Normal — los logs aparecen todos al terminar. Para ver progreso en tiempo real: ejecutar sin `> archivo.txt` |

---

*Documento generado el 2026-08-15. El proyecto está en producción activa.*
