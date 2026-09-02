# Traspaso maestro — SEACE Monitor

Contexto completo para retomar el proyecto **sin chat previo**.
Snapshot: **1 sep 2026** (Perú). Un solo punto de entrada; el detalle vive en los docs enlazados.

| Doc | Para qué |
|---|---|
| [ARQUITECTURA_TECNICA.md](./ARQUITECTURA_TECNICA.md) | Cómo/por qué de cada pieza, con evidencia. §§K–L = B20 + observabilidad |
| [ESTADO_CIERRE_2026-08-29.md](./ESTADO_CIERRE_2026-08-29.md) | Foto de iteraciones 1–11 (self-routing, funnel, conversión 30d) |
| [ESTADO_CIERRE_2026-08-20.md](./ESTADO_CIERRE_2026-08-20.md) | Foto histórica de iteraciones 1–9 + fixes |
| [CHANGELOG_ITERACIONES.md](./CHANGELOG_ITERACIONES.md) | Iteraciones 1–11 + clasificación IT 1 sep: qué, archivos, commit, estado |
| [CRITERIOS_DECISION_ENERTRONIC.md](./CRITERIOS_DECISION_ENERTRONIC.md) | Inteligencia de negocio. §3 «la IA busca» = intención, no implementación |
| [PLAN_DE_TRABAJO.md](./PLAN_DE_TRABAJO.md) | Plan v2 **parcialmente desactualizado** — ver §0 y §7 |

**Regla:** el código gana al PLAN. Secretos solo por **nombre** (nunca JWT, API keys ni tokens).

Este archivo + los enlaces de la tabla bastan para retomar. `DOCUMENTACION.md` y el README raíz del monitor describen el prototipo viejo (scrape 06:00, Llama); **no** empieces por ahí.

---

## 0. Cómo usar este documento

### Qué es SEACE Monitor (3 frases)

Sistema que deja de ser un buscador del SEACE y pasa a ser **asesor de licitaciones menores (≤ 8 UIT, techo S/42 800 en 2026)** para **ENERTRONIC** (TI peruana: IA, cloud, desarrollo, telemetría). Ingesta diaria el corpus público, rankea oportunidades sin IA (#9), analiza un TDR con Gemini incluyendo razonamiento de 2º orden (#10) y recálcula escenarios sobre ese análisis congelado (#11, panel + streaming). El humano pone el número final; la IA no oculta contratos ni cifra seca.

### Los 4 repos / carpetas

Ruta local base: `d:\ROLANDO\DEV_APPS\seace8uit\`

| Repo | Qué hace | Prod |
|---|---|---|
| `seace-monitor` | Pipeline Python: ingesta SEACE → G1 estados → PDF/OCR → chunk → embed Gemini. SQL, evals, docs. | Job `pipeline.yml` (cron GHA **respaldo** + `workflow_dispatch`) |
| `seace-web` | SPA autenticada (Ruta del día, análisis, chat, observabilidad, login) | https://seace.rdiaz-lab.xyz |
| `seace-ai-proxy` | Worker: RAG v2, `/analizar`, `/cotizar`, `/admin/stats`, funnel KV, cupos | https://seace-ai-proxy.rdiazg14.workers.dev |
| `seace-pipeline-trigger` | Worker CF: Cron 14:00 UTC → `workflow_dispatch` de `pipeline.yml` | https://seace-pipeline-trigger.rdiazg14.workers.dev |

El front **no** llama a Gemini. Lee Supabase (anon + RLS) y pega al Worker. El pipeline **escribe** con service role. El Worker Gemini **lee** con anon y **escribe** `cotizar_tipo_log` / lee `perfiles` (admin) con `SUPABASE_SERVICE_KEY`. El service_role **nunca** va al JS público.

### Método de trabajo

- **Expand-contract (D5):** columnas/RPC nuevas en paralelo; no dropear v1 hasta Fase 7. El RAG nunca queda a medias.
- **Probar chico** (dry-run, `--limit`, un contrato) → medir → recién entonces cron/prod. **Excepción:** `clasificar_gemini.py` no es determinista (`temperature: 0` igual). Un dry-run limpio **no** garantiza el UPDATE real; revisar el SELECT a mano antes de confiar.
- **Medir, no adivinar:** eval G4 (`data/eval_v2.json`, success@10 **0.633**). Etiquetas en arquitectura: MEDIDA vs POR-DEFECTO.
- **Puntos de control:** Rolando aprueba antes de cron, deploy masivo u OCR caro.
- **Calibración vs código:** el asistente (Rolando) define criterio de negocio; Cursor implementa. Criterios ENERTRONIC mandan sobre ocurrencias del LLM.

Al retomar: leé **§6 (cierre 30–31 ago + clasificación 1 sep)** + §7 (gotchas) + [CHANGELOG_ITERACIONES.md](./CHANGELOG_ITERACIONES.md). No reabras retrieval v2 ni Auth salvo pedido explícito. No diseñes B13/B14 hasta confirmar B20 estable. **No** metas `clasificar_gemini.py` en el cron: hace falta Arquitectura C (sesión aparte).

---

## 1. Mapa de los 4 repos

Hashes de este corte (HEAD `origin/main` al 1 sep 2026):

| Repo | GitHub | Visibilidad | Rama | HEAD |
|---|---|---|---|---|
| seace-monitor | https://github.com/rdiazg14/seace-monitor | **público** | `main` | `d430272` |
| seace-web | https://github.com/rdiazg14/seace-web | **público** | `main` | `8a0b596` |
| seace-ai-proxy | https://github.com/rdiazg14/seace-ai-proxy | **privado** | `main` | `da3caf8` · Worker CF `cbf31b49-e3e7-44b0-a8cf-6cd4f5113ad4` |
| seace-pipeline-trigger | carpeta local (mismo account CF) | — | — | Worker `seace-pipeline-trigger.rdiazg14.workers.dev` |

### 1.1 seace-monitor

**Stack:** Python 3.12, Playwright, supabase-py, httpx, PyMuPDF, pydantic. GitHub Actions. Local: `.env` (no versionado) con `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `GEMINI_API_KEY`. Scripts documentan `uv run python …`; el cron usa `pip install -r requirements.txt`. No hay `pyproject.toml`.

**Carpetas / archivos clave**

| Path | Rol |
|---|---|
| `ingesta_completa.py` | Altas incrementales + keywords `IT_CATS` / `relevancia_ia` |
| `reclasificar_categoria.py` | Backfill keywords sobre ambas columnas NULL (no Gemini) |
| `clasificar_gemini.py` | Fase B: `categoria_it` con Flash sobre nulls. **No** está en el cron |
| `refresh_estados.py` | G1 frescura de estado (`--gc` existe, **cron no lo pasa**) |
| `enriquecer_detalle.py` | Área + ítems CUBSO |
| `descargar_requerimiento.py` | PDF nativo + OCR selectivo |
| `chunker_contratos.py` | Chunks 800/500, sin overlap |
| `generar_embeddings.py` | Solo `embedding_v2`, `RETRIEVAL_DOCUMENT` + L2 |
| `alerta_g3.py` | Issue si un paso del job falla (`--paso funnel` incluido) |
| `reconciliar_funnel.py` | GET Worker → upsert marcas `analizado`/`cotizado` (ISO del KV, no `now()`) |
| `eval_retrieval.py` | G4 híbrido **pre-rerank** |
| `buscar_tdr_v2.sql` | RPC vector 1536 |
| `.github/workflows/pipeline.yml` | Job diario (schedule GHA = respaldo; disparo primario = CF) |
| `docs/` | Traspaso, arquitectura, cierres 20/29-ago, changelog, criterios, PLAN, SQL funnel/conversión/`cotizar_tipo_log` |
| `capa_semantica.sql` | Vistas Dashboard (iter. 9). Ya aplicado. **No** incluye `v_kpis_conversion` |
| `docs/migracion_funnel_conversion.sql` | Columnas `analizado`/`cotizado`/`fecha_*`. Ya aplicado |
| `docs/vista_kpis_conversion.sql` | `v_kpis_conversion` + `_rubro`. Ya aplicado (GRANT anon) |
| `docs/cotizar_tipo_log.sql` | Tabla + RLS admin SELECT. Ya aplicado |
| `docs/cotizar_tipo_log_select_admin.sql` | GRANT + policy apply-only. Ya aplicado |

**Correr local (ejemplos reales de los scripts):**

```bash
uv run python refresh_estados.py --dry-run --limit 30
uv run python descargar_requerimiento.py --solo-nativo --limit 5
uv run python chunker_contratos.py --solo-pdf --solo-nuevos
uv run python generar_embeddings.py --backend gemini --ids 87164 --fuente pdf
uv run python eval_retrieval.py --backend v2
uv run python reclasificar_categoria.py --dry-run --limit 200
uv run python clasificar_gemini.py --dry-run --limit 30 --filtro vigentes
```

**Deploy:** no hay servidor. Push a `main` no despliega el pipeline. El disparo **primario** es el Worker `seace-pipeline-trigger` (cron CF 14:00 UTC → `workflow_dispatch`). El `schedule: 0 14 * * *` de `pipeline.yml` queda como respaldo. `workflow_dispatch` también permite OCR/skip/dry-run a mano.

Workflows: `pipeline.yml` (activo, schedule **respaldo** + dispatch) · `embed_v2.yml` / `pdf.yml` (solo manual, one-shot) · `scrape.yml` **DEPRECATED** (inactivo ~16 ago). El README raíz del repo **sigue describiendo scrape.yml como diario 06:00** — ignorarlo.

### 1.2 seace-web

**Stack:** React 19 + Vite 8 + TypeScript + Tailwind 3 + react-router 7 + supabase-js. Host: **GitHub Pages** (no Cloudflare Pages), dominio `seace.rdiaz-lab.xyz`.

**`src/` clave:** `App.tsx` rutas · `lib/supabase.ts` URL + `AI_PROXY` · `lib/rutaDia.ts` score #9 + `esPostulable` · `lib/capaSemantica.ts` Dashboard + `cargarKpisConversion` · `lib/analisis.ts` tipos #10/#11 · `pages/RutaDia.tsx` · `AnalisisContrato.tsx` (página #10 + panel #11) · `Observabilidad.tsx` · `Dashboard.tsx` (capa + conversión 30d) · `components/AnalisisV2.tsx` · `TimelineFishbone.tsx` · `ChatTable.tsx` / `ChatChart.tsx` · `Pills.tsx` (`CatItIaPill`) · `Chat.tsx` (RAG) · `Login.tsx` · `RequireAuth.tsx` · `supabase/perfiles.sql` + Edge Functions `crear-usuario` / `desactivar-usuario`.

**Local:**

```bash
npm install
npm run dev          # Vite, puerto 5173 (CORS del Worker lo permite)
```

`npm run build` → `tsc -b && vite build`. El workflow copia `dist/index.html` a `dist/404.html` (SPA en Pages).

**Deploy:** push a `main` → `.github/workflows/deploy.yml` (`Deploy seace-web → GitHub Pages`). HEAD de este corte: `8a0b596`.

### 1.3 seace-ai-proxy

**Stack:** Cloudflare Workers, TypeScript, Wrangler 4. Bindings: `AI`, KV `CHAT_LIMITS`, rate limit `CHAT_RPM`.

**`src/`:** `index.ts` (router + RAG) · `analizar.ts` · `cotizar.ts` · `adminStats.ts` · `escenario.ts` · `funnel.ts` · `history.ts` · `limits.ts`. Config: `wrangler.toml`. Secrets en CF, no en git: `.dev.vars` local (gitignored).

**Local:**

```bash
npm install
npx wrangler dev     # secrets desde .dev.vars
```

**Deploy:**

```bash
npx wrangler deploy
```

No hay Action de deploy del Worker: es manual (`npx wrangler deploy` desde esta carpeta). Versión viva: `npx wrangler deployments list` → este corte **`cbf31b49-e3e7-44b0-a8cf-6cd4f5113ad4`** = git `da3caf8`.

Router (`index.ts`): **GET** `/funnel-pendientes` (Bearer `FUNNEL_TOKEN`; no es para el front) y **GET `/admin/stats`** (JWT de sesión + `perfiles.rol === 'admin'`). El resto es **POST**. `/embed` → 410 · `/analizar` · `/cotizar` (JSON o SSE) · resto = chat RAG (`query` + `history?`). SSE si `Accept: text/event-stream`.

### 1.4 seace-pipeline-trigger

Worker **separado** de `seace-ai-proxy`. Cron Cloudflare `0 14 * * *` → API GitHub `workflow_dispatch` sobre `pipeline.yml`. Secrets: `GITHUB_PAT`, `TRIGGER_TEST_TOKEN` (Rolando, `wrangler secret put`). KV: el mismo `CHAT_LIMITS`; clave `pipeline-trigger:last-error` si el dispatch falla. `POST /` de prueba con `TRIGGER_TEST_TOKEN`. Detalle: arquitectura §K.

El `schedule:` de `pipeline.yml` **no se apagó**. B20 **no** está 100 % cerrado: falta ver 2–3 días de `gh run list` con horario sano.

### Orden obligatorio: Worker Gemini **antes** que Pages

El front **hardcodea** `AI_PROXY = https://seace-ai-proxy.rdiazg14.workers.dev` (`seace-web/src/lib/supabase.ts` L7). Un Pages nuevo que llame `/cotizar` contra un Worker viejo da 404. Un Worker nuevo con Pages viejo sigue sirviendo el chat. Por eso: **deploy Worker → smoke de endpoints → push/Pages**.

---

## 2. Infraestructura y servicios

### 2.1 Supabase — el único proyecto en código

- **Ref activo:** `wusywwhcyqngnpvpzxyr`  
  URL: `https://wusywwhcyqngnpvpzxyr.supabase.co`  
  Región documentada: sa-east-1. `seace-web/supabase/config.toml` L1.
- **Proyecto inactivo:** Rolando indicó que existe **otro** proyecto Supabase que **no se usa**. En este workspace **no aparece un segundo `project_id`**. No linkear CLI, no correr SQL, no rotar keys, en ningún ref que no sea `wusywwhcyqngnpvpzxyr`. El ref del inactivo: **[por confirmar con Rolando]**.

**Tablas**

| Tabla | Uso |
|---|---|
| `contratos` | Ficha SEACE. PK = `id` SEACE. IT: `categoria_it`, `relevancia_ia`. Funnel: `analizado`, `cotizado`, `fecha_analisis`, `fecha_cotizacion` (permanentes; FALSE = nunca marcado desde que existe la columna). Extra: `tdr_texto`, `pdf_hash`, `pdf_descargado`, `req_url` (`sin_pdf` = sin anexo PDF), `tdr_tipo_extraccion`, `paginas_ocr_*`, `estado_verificado_at`, `nom_area_usuaria`, `items_json` |
| `chunks_tdr` | `texto`, `embedding vector(768)` **v1**, `embedding_v2 vector(1536)`, `fuente` api\|pdf, `meta_entidad` / `meta_nro` |
| `perfiles` | `rol` admin\|normal, FK `auth.users` (`seace-web/supabase/perfiles.sql`) |
| `ingesta_rechazados` | G2 dead-letter |
| `cotizar_tipo_log` | MISS de `/cotizar`. INSERT service_role (Worker). SELECT solo admin (`es_admin()`) |

**RPC:** `buscar_tdr_v2` (1536, default min_sim 0.20) · `buscar_contratos` (FTS) · `buscar_tdr` (768, **no** lo usa el chat v2).

**RLS:** `contratos` SELECT `anon`+`authenticated` (datos públicos SEACE). `chunks_tdr` SELECT abierto. Escritura: service role del pipeline. `perfiles`: propio o admin (`es_admin()`). `cotizar_tipo_log`: SELECT admin; INSERT no hay policy para `authenticated` (Worker bypasea con service_role).

**Edge Functions** (verify JWT): `crear-usuario`, `desactivar-usuario`. Usan `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` en el entorno de Functions (`functions/_shared/admin.ts`). No están en git.

**Keys (nombres):** pipeline GitHub `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` · Worker Gemini secrets `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_KEY`, `FUNNEL_TOKEN`, `GEMINI_API_KEY` · SPA: la anon key va **compilada en el JS público** (lectura RLS; no da escritura). Nunca commitear service_role.

### 2.2 Gemini (AI Studio)

| Uso | Modelo | Dónde |
|---|---|---|
| Embeddings | `gemini-embedding-001` @ **1536**, L2, QUERY vs DOCUMENT | Worker query RAG (1 por pregunta, **sin caché**); pipeline documentos (`batchEmbedContents`, lote 16) |
| Generación | `gemini-3.7-flash`, thinking `LOW` | Chat RAG (filtros + generate), `/analizar` (1× MISS), `/cotizar` (**1×** generate en MISS; HIT = 0), OCR visión (cron) |

Secret: `GEMINI_API_KEY` — Cloudflare Worker **y** GitHub Actions (mismo nombre). No en `wrangler.toml`.

**Tope de gasto:** S/10/mes operado en **AI Studio** (no hay código que lo corte). Complementa los cupos del Worker. Monitoreo: consola AI Studio + KV `flash:` / `analyze:` / `cotizar:`.

Tier exacto de billing: **[por confirmar con Rolando]** (en sprints se habló de billing activo vs free; el backstop que manda es el S/10).

### 2.3 Cloudflare

- Account id (toml): `5a2b884f36bd62011960b879c3737546`
- Workers: `seace-ai-proxy` (Gemini) y `seace-pipeline-trigger` (solo dispatch)
- Workers AI: **solo** reranker `@cf/baai/bge-reranker-base` (no embed BGE; `/embed` = 410)
- KV `CHAT_LIMITS` id `c63cfd497041477f91dafdde5935f37d` — **único** KV, **compartido** por ambos Workers: cupos, caché `analyze:` / `chat:`, marcas `funnel:`, `pipeline-trigger:last-error`
- Rate limiter `CHAT_RPM` namespace `8701`, **8 / 60 s**, keys distintas por flujo (`ip:`, `analyze:ip:`, `cotizar:ip:`)

**Vars no secretas** (`wrangler.toml`):

| Var | Valor prod |
|---|---|
| `SUPABASE_URL` | `https://wusywwhcyqngnpvpzxyr.supabase.co` |
| `RAG_BACKEND` | `v2` |
| `GEMINI_FLASH_MODEL` | `gemini-3.7-flash` |
| `SIMILARITY_THRESHOLD` | `0.70` (v1, apagado) |
| `SIMILARITY_THRESHOLD_V2` | `0.20` |
| `CHAT_RPD` | `40` |
| `FLASH_RPD` | `200` |
| `ANALYZE_IP_RPD` | `15` |
| `ANALYZE_RPD` | `40` |
| `COTIZAR_IP_RPD` | `20` |
| `COTIZAR_RPD` | `80` |

**Secrets (solo nombre) — seace-ai-proxy:** `GEMINI_API_KEY`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_KEY`, `FUNNEL_TOKEN`.

**Secrets (solo nombre) — seace-pipeline-trigger:** `GITHUB_PAT`, `TRIGGER_TEST_TOKEN`. Los puso Rolando; Cursor no los carga.

CORS orígenes: `https://seace.rdiaz-lab.xyz`, `https://rdiazg14.github.io`, `http://localhost:5173`. Métodos: GET, POST, OPTIONS. Headers extra: `Authorization`, `X-Funnel-Token`.

### 2.4 GitHub Actions

| Workflow | Cuándo | Secrets (nombre) |
|---|---|---|
| `pipeline.yml` | `schedule` 14:00 UTC (**respaldo**) + `workflow_dispatch` (primario: CF trigger) | `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `GEMINI_API_KEY`, `FUNNEL_TOKEN` (+ `GITHUB_TOKEN` G3) |
| `embed_v2.yml` | manual | mismos Gemini/Supabase |
| `pdf.yml` | manual (histórico; PDF ya está en el cron) | idem |
| `scrape.yml` | solo dispatch, deprecado | `GITHUB_TOKEN` |
| `seace-web` `deploy.yml` | push `main` | Pages (OIDC), **sin** Gemini |

---

## 3. El producto — qué ve y hace el usuario

Login: `/login` → `signInWithPassword`. Signup público **cerrado**. Roles `admin` \| `normal`. Altas: página `/usuarios` (admin) → Edge Functions. Home de la app sigue siendo `/` Dashboard; la pieza de trabajo diario es `/ruta-dia`.

Criterios (resumen; detalle en [CRITERIOS_DECISION_ENERTRONIC.md](./CRITERIOS_DECISION_ENERTRONIC.md)):

- **Regla de oro:** la IA rankea y aconseja; **nunca oculta**; el humano cotiza.
- Rubros: Núcleo (IA/cloud/dev/OT) > Adyacente > Oportunista > Marginal (hardware no se descarta).
- Techo 8 UIT. Economía siempre con supuestos. Firma digital ≠ tokens de IA (no sube a Núcleo por ALTA sola).

### #9 Ruta del día

- **UI:** https://seace.rdiaz-lab.xyz/ruta-dia  
- **Worker:** ninguno. Score 100% en el browser (`rutaDia.ts`).  
- **Datos:** `estado IN (Vigente, En Evaluación)` + IT/IA (`RutaDia.tsx` fetchUniverso). Culminado no entra.  
- **Fórmula:** rubro 50 + vigencia 25 + urgencia 15 + señales 10. Urgencia **2–7 d = 15 > hoy = 10**. Overlay OT/telemetría → núcleo; nunca degrada.  
- **Postulabilidad (única, `cffcc2b`):** `esPostulable()` = `estado==='Vigente'` y (`fecha_fin` null o `>= hoy` Lima). Ranking default y brief = solo postulables. Chip «En evaluación / cerrados» para el resto. KPIs (incl. «Nuevos hoy») cuentan postulables.  
- Click → `/analisis/:id`.

### #10 Análisis de contrato

- **UI:** `/analisis/:id` — infografía, N alternativas, economía por componente, contradicciones, fishbone, panel #11.  
- **Worker:** `POST /analizar` `{ contrato_id }`  
- **Flujo:** ficha (`tdr_texto` / `pdf_hash`) → TDR columna o chunks (máx 80) → si chars &lt; **200** → **422** `sin_tdr` (sin cupo, **no** marca funnel) → KV `analyze:{id}:{hash}` TTL 3 d (HIT = 0 Gemini, **sí** marca `funnel:analizado`) → cupos ANALYZE → 1 Flash JSON → post-proceso (`completarMomentoDia`, `asegurarRecomendada`, `alinearEconomiaConAlternativa`) → KV.put + marca funnel. Techo S/42 800.  
- Schema: 5 secciones base + `timeline.hitos`, `viabilidad.{ratio_alcance, cotizacion_por_componente, contradicciones_tdr}`, `alternativas[]`, `estructura_contractual`, `componentes_servicio`, `requisitos_proveedor`, `riesgos_contractuales`, `chips_sugeridos`, consorcio tri-estado.  
- 502: cuerpo `analisis_fallido` + banner amable + Reintentar. Cupo **sí** se gastó. Funnel **no** se marca. Análisis **congelado**.

### #11 Cotización asistida

- **UI:** panel lateral en `/analisis/:id` (default 380 px, resizable 320–720, `key={contratoId}`). Desktop ≥1024 px **abierto** al cargar; móvil cerrado + FAB. Persistencia `localStorage chat_escenarios_{id}`.  
- **Worker:** `POST /cotizar` `{ contrato_id, query, history? }` (SSE si `Accept: text/event-stream`)  
- **Flujo:** misma clave KV #10 → si no hay caché **409** `sin_analisis` → caché exacta `chat:{id}:{pdf_hash}:{sha256}` (HIT = 0 Gemini) → si MISS: **1** Flash generate (self-routing por `tipo_respuesta` **y** `necesita_internet` en el mismo schema; fail-closed en post-proceso) → `cotizar_tipo_log` fail-soft → `KV.put` chat solo si `esCacheable` → marca funnel independiente de cacheable. **No** reescribe el análisis. **No** RAG. **No** busca precios en internet.  
- Si `necesita_internet === true`: botón **«Buscar en TDRs relacionados»** → `/chat` (encapsulamiento deliberado: no mezclar análisis congelado con RAG general). El Chat RAG es corpus SEACE, no la web.  
- Por qué no reusar el chat RAG in-panel: contaminaría el análisis congelado. Ver arquitectura §F.

### Chat RAG + memoria #3

- **UI:** `/chat`  
- **Worker:** `POST /` `{ query, history? }` (opcional SSE)  
- Front manda últimos 4 pares, máx 8 ítems × 500 chars (`Chat.tsx`). Worker `sanitizeHistory`; `historyBlock` antepone `Conversación reciente:` **después** del retrieve.  
- Si el stream de Gemini no produce texto (p. ej. thinking sin tokens): fallback **«No pude generar una respuesta.»** (no caja vacía).  
- **Limitación:** embed/filtros/RAG ven **solo la query actual**. No hay internet real. Reescribir query con Flash **no está**.

### Páginas legacy (candidatas a jubilar)

| Ruta | Estado |
|---|---|
| `/` Dashboard | Iter. 9: lee `v_kpis_*` / `v_contratos_estado` (fallback TS). Iter. 11: bloque conversión 30d (`v_kpis_conversion`, falla suave → "Sin datos"). Filtros de urgencia **sin** leak de vencidos. Sigue siendo el **home**. |
| `/buscar` | FTS + chips. Un badge `CatItIaPill` (IT o IA, no ambos). |
| `/docs` | Notas de API. |
| `/usuarios` | Solo admin. |
| `/observabilidad` | Solo admin. KV de hoy + `cotizar_tipo_log` 14 d + last-error. Link a Dashboard para `v_kpis_*`. |

Cuando Ruta del día sea el home, Dashboard/Buscador se pueden apagar. No bloquea.

---

## 4. Retrieval / RAG y pipeline

Detalle, números y «por qué»: [ARQUITECTURA_TECNICA.md](./ARQUITECTURA_TECNICA.md) §§A–C.

**RAG v2 (prod `RAG_BACKEND=v2`):** extraer filtros (1 Flash) → embed query 1536 L2 → paralelo `buscar_tdr_v2` (20, umbral 0.20) + FTS (20) → RRF k=60 a **nivel contrato** → ≤2 chunks/contrato → rerank `-base` top 5 → Flash. Eval G4: **27% → 63% success@10 sin reranker**.

**Pipeline diario (09:00 Perú):** el disparo que se pretende honrar es Cloudflare Cron → `workflow_dispatch`. El `schedule:` de GHA **se atrasó ~9 h 20 m** el 27 y 28 ago (contrato **90383** perdió ventana). Pasos del job: ingesta (keywords IT) → G1 **sin `--gc`** → detalle → PDF `--solo-nativo` → OCR `--solo-ti` 2 h / rpm 6 → chunk api + pdf `--solo-nuevos` → embed Gemini `WHERE embedding_v2 IS NULL` → **reconciliar funnel** (`continue-on-error`) → G3. **`clasificar_gemini.py` no está en el yaml** (manual; hueco post-detalle / pre-OCR). Escala ancla (~76k contratos): si crece 10×, reabrir índice/umbral/chunk/OCR/FLASH_RPD.

---

## 5. Control de gasto (no romper)

Tres sistemas **aislados** (un flujo no descuenta al otro). Periodo RPD = **UTC**. RPM = binding atómico; RPD = KV get+put (no transacción).

| Sistema | RPM key | RPD IP | Global | Gemini |
|---|---|---|---|---|
| Chat | `ip:{ip}` 8/60 | `ip:{ip}:{day}` 40 | `flash:{day}` **200 Δ2** (extract+generate; embed **no** cuenta en Δ2) | |
| `/analizar` | `analyze:ip:{ip}` | `analyze:ip:{ip}:{day}` **15** | `analyze:{day}` **40** | 1 generate si MISS; HIT/422 = 0 |
| `/cotizar` | `cotizar:ip:{ip}` | `cotizar:ip:{ip}:{day}` **20** (1 pregunta) | `cotizar:{day}` **80 Δ1** | HIT=0 Gemini; MISS=1 generate; 409=0 |

**Qué gasta Gemini además:** OCR del cron (rpm 6, tope 2 h, `--max-ocr-dia 6000`); embeddings del pipeline; `clasificar_gemini.py` si se corre a mano (misma API key, **sin** cupo KV ni `flash_ocr_cuota.json`).

**Backstop:** S/10/mes AI Studio. Si el Worker no corta, la consola sí.

**502:** `/analizar` HTTP 502 `{ error: 'analisis_fallido', mensaje, reintentar, detalle_tecnico }` (banner en el front; visto históricamente contrato **66461** como JSON crudo). `/cotizar`: 502 JSON si falla antes del stream; si ya stremea, evento `error` (HTTP 200). Chat JSON: HTTP **200** con texto amable. Filtros v2: fallback regex, no 502. Cupo ANALYZE se cobra **antes** de Gemini. Detalle: arquitectura §G.

---

## 6. Estado actual y mejoras pendientes

### En producción (base, iter. 1–11 + clasificación 1 sep)

Asesor #9 + #10 + #11 (self-routing, streaming, caché exacta) · funnel KV→PG · Dashboard conversión 30d · capa semántica · RAG v2 · memoria #3 · login · **cascada `categoria_it`** (keywords en ingesta + Gemini manual). Foto iter. 1–11: `ESTADO_CIERRE_2026-08-29.md`. Calibración: **87880** SEDAPAR, **87502** CENEPRED.

No reabrir salvo pedido: cutover embed 768, Auth, `RAG_BACKEND=v2`, schema 2º orden de #10, orden del cupo ANALYZE, clasificador Flash de `/cotizar` (borrado a propósito). **Sí** se puede reabrir el *cómo* se dispara el cron (B20): el `schedule:` de GHA ya no se trata como reloj fiable.

### Cierre 30–31 ago 2026

Auditoría de confianza + Paquete C + B20 + routing RAG + B4 fase 1. Hechos verificados, no un cierre “todo verde”.

**Completado**

| Ítem | Resultado |
|---|---|
| B1 | Sano. El filtro de postulables no oculta. Mercado sub-8-UIT escaso. 4 queries SQL de solo lectura. |
| B2 | Sano *en ese momento*: refresh &lt;15 h, cron con datos frescos. |
| B3 | Confirmado en prod: MISS ~12 s → HIT ~0.4 s, cuerpos idénticos, 0 Gemini en HIT. |
| B20 | Worker `seace-pipeline-trigger` en prod. Run **33319218551** = `workflow_dispatch` desde Cloudflare. **No** 100 % cerrado: observar 2–3 días más de `gh run list`. |
| Paquete C | B5 (parcial: residual de badge vs auto-scroll en stream largo, riesgo bajo aceptado), B6 (panel 320–720, localStorage), B7 (revert del over-fix; trace persistente), B9 (`esPostulable` en Buscador), B10 (`cotizar_tipo_log`, insert prod contrato **90403** id=1), B11 (no era bug: claves `analyze:` vs `chat:`; what-if con supuestos no cacheable — cerrado sin código). |
| Routing RAG | `necesita_internet` lo declara el modelo (schema + fail-closed). Botón «Buscar en TDRs relacionados» → `/chat`. Texto del modelo: «No tengo internet.» Fallback de stream vacío en Chat. |
| B4 fase 1 | `/observabilidad` + `GET /admin/stats` (JWT + rol admin server-side). Curl: 401 / 401 / 405. |

**Medido, no resuelto — B12**

27.4 % de contratos IT en 60 días con ventana &lt;24 h (**118/430**). ~13 % de esos eran Núcleo. Casos: **90326**, **90083**. B13/B14 (alerta + frecuencia del pipeline) **bloqueados** hasta B20 estable.

**Redefinido — B15**

«Precios cloud reales» era un nombre angosto: el `costo_estimado_soles` de #10 cubre cualquier rubro y hoy el modelo estima a ojo. CRITERIOS §3 decía «la IA busca»; **nunca se implementó**. B15 se fusiona con búsqueda web real; no se construye una tabla estática de precios. Pendiente de dimensionar (costo de API, latencia, riesgo de contaminar el análisis congelado).

**Dato B1 (histórico 30 ago):** ~2.5 % (**44/1719**) de Vigente con `categoria_it`. El 1 sep se atacó la fuga (keywords + Gemini); **no** se re-midió ese denominador. El filtro de postulables **no** cambió.

### Clasificación IT — 1 sep 2026

Cascada en prod. Detalle: [ARQUITECTURA_TECNICA.md](./ARQUITECTURA_TECNICA.md) §C.

**Síntoma (fuga):** IT visible en el Buscador y **ausente** de Ruta del día. Caso: **90432** / CM-6-2026-HNSEB («implementación de software»). Ruta filtra `categoria_it` OR `relevancia_ia` NOT NULL; sin etiqueta no entra. La hipótesis «el OCR no clasifica» se **descartó**: las keywords corren una vez en la ingesta y no leen TDR.

**Ejes independientes:** `categoria_it` = ¿es TI? (13 líneas; **solo 1** es IA/analytics). `relevancia_ia` = ¿tiene IA? El clasificador pregunta «¿es TI en cualquiera de las 13?», no «¿tiene IA?». Los rescates 90432 (desarrollo) y 90331 (redes) **no** eran IA.

| Ítem | Resultado |
|---|---|
| Fase A keywords | `IT_CATS` Desarrollo software + `implementacion de software` (única keyword limpia; el resto se midió y se descartó por FP). `reclasificar_categoria.py` (solo ambas NULL). 3 UPDATE: **28275**, **31625**, **90432**. Commits `1004b02`, `24a2a3b`. |
| Fase B Gemini | `clasificar_gemini.py`: batch Flash + schema enum 13 + `ninguna`. Solo nulls; no pisa keywords; no escribe `relevancia_ia`; no toca cuota OCR. **Herramienta manual.** Commit `d430272`. |
| Corrida vigentes | `temperature: 0` igual. Dry-run → 1; escritura real → **3**. Quedó **90331** Redes/cableado («comunicación privada»). Revertidos a NULL **90592** (tóner→Hardware) y **90386** (CPU autómata diésel→Hardware). |
| Qué no hace | Incremental no re-etiqueta. Keywords no leen TDR / ítems / área. OCR `--solo-ti` exige etiqueta (huevo-gallina). JWT admin no UPDATE `categoria_it`. |

**Lección (leer antes de tocar clasificación):** Gemini **no** es determinista. Un dry-run limpio no autoriza un backfill. Revisión humana del SELECT **obligatoria** antes de confiar. **No** automatizar sin Arquitectura C.

**Arquitectura C (diseñada, no implementada; sesión aparte):** keywords en tabla de config + Gemini con confianza declarada + desempate para dudosos + cola de revisión admin (fases C1–C4). Hasta entonces `clasificar_gemini.py` queda **manual**.

**Próximo paso recomendado**

1. Observar B20 2–3 días (`gh run list`, horario).  
2. Recién entonces retomar **B13/B14**.  
3. **B4 fase 2** (tokens Gemini / costo en soles): mismo cuidado que B20; hoy **no** hay `usageMetadata`.  
4. Clasificación automática = **Arquitectura C**, no «meter el script actual en el cron». No ampliar keywords sucias (`software`, `sistema`, `TI`).

### Backlog (todo opcional)

| Ítem | Esfuerzo | Valor | Notas |
|---|---|---|---|
| Observar B20 | Operación | Reloj | 2–3 días de `gh run list` |
| B13 / B14 | Medio | Ventanas &lt;24 h | **Bloqueado** por B20 |
| B4 fase 2 | Medio | Costo real | Instrumentar Gemini; no mezclar con observabilidad de KV |
| B15 / búsqueda web | Alto (dimensionar) | Economía #10 | Fusionado; no tabla estática |
| Cobertura `categoria_it` / **Arquitectura C** | Alto | Anti-drift | Diseñada, **no** implementada. No cablear el script actual al cron |
| B17 auto-postulación | Alto | Producto | Análisis profundo; **alto riesgo legal**. No tocado |
| B18 firma blockchain | Investigar | Mercado | Medir corpus antes de construir. No tocado |
| B19 2ª fuente SEACE | Alto | Cobertura | prod4 / OpenEgocio; hay bot-detection. No tocado |
| Home = Ruta del día | Bajo | Enfoque | `/` = Dashboard |
| Aligerar `v_kpis_dashboard` | Bajo–medio | Estabilidad | timeout 57014; fallback TS |
| Caché semántica `/cotizar` | Medio | Tokens | Hoy solo exacta + `esCacheable` |
| No cobrar ANALYZE si 502 | Medio | Honestidad de cupo | Deliberado |
| **#4 chunking** | Eval offline | Fruta POR-DEFECTO | **Sigue abierto.** Overlap + tamaño vs 63 %. |
| **Fase 7** drop BGE 768 | SQL + smoke | Limpieza | Chat v2 no los usa |
| **#12 brief diario** | Medio | Producto | No existe |
| Chat que lea KPIs SQL | Medio | Asesor | Fuera de iter. 9 |
| Reranker v2-m3 | Eval **post-rerank** | Precisión | **Sigue abierto.** PLAN vs `-base`. |

**Punto de entrada para optimizar retrieval:** reranker `-base` · threshold **0.20** · RRF **k=60** · chunk **800/500 sin overlap** → #4 vs 63 %. El 30–31 ago **no** tocó retrieval.

---

## 7. Gotchas (un chat nuevo no debe malinterpretar)

1. **«Vigente» ≠ ventana de cotización abierta.** G1 copia `idEstadoContrato` del SEACE (2 Vigente / 3 En Evaluación / 4 Culminado). `esPostulable()` exige Vigente **y** ventana Lima. `rankingActivo` **no** recorta (deja vencidos y En Evaluación); el **chip** default sí. No «arreglar» G1 para cerrar por `fecha_fin`.
2. **422 `/analizar` = TDR &lt; 200 chars**, no `req_url=sin_pdf`. Un sin_pdf **con chunks API** se analiza (p. ej. 83729 → 200). 422 real: ficha sin texto y sin chunks (p. ej. id 42).
3. **Solo un Supabase en código:** `wusywwhcyqngnpvpzxyr`. Otro proyecto inactivo: no tocar. Ref: [por confirmar con Rolando].
4. **Restos v1 en el Worker:** constante Llama `@cf/meta/llama-3.3-70b-instruct-fp8-fast`, `retrieveContext` 768, `RAG_BACKEND` default **v1 si falta env** (`index.ts` L131–132). Prod fija `v2` en toml. No usar `wrangler rollback` a v1 sin pedido. Fase 7 limpia BD; el bundle v1 se puede borrar después.
5. **`POST /embed` = 410.** El pipeline **no** embebe por el Worker.
6. **Eval 63% no incluye reranker** (`eval_retrieval.py` L334–335). No citar 63% como calidad post-rerank.
7. **PLAN vs realidad:** reranker v2-m3, chunks 200–400+overlap, GC al cerrar, costo $0/mes, checklist Gemini vacío, Fase 2 PDF «blocker» — **desactualizado**. Lista en arquitectura «Discrepancias». El PLAN §8 checklist no refleja que Gemini ya está en Actions y CF.
8. **README de seace-monitor** describe scrape 06:00 y CSV token como el producto. El diario es `pipeline.yml` disparado por CF (schedule GHA = respaldo). Empezá por este traspaso, no por el README.
9. **README del Worker** describe RAG + `/analizar` + `/cotizar` + funnel. Si el README y el código divergen, gana el código. Cupos ANALYZE/COTIZAR/FLASH están **aislados**.
10. **Home ≠ Ruta del día.** `/` es Dashboard (`App.tsx`). Navbar pone «Ruta del día» primero; el logo SEACE apunta a `/`. `path="*"` redirige a `/`.
11. **RPD en UTC, ranking en día Lima.** Un cupo «diario» cambia a las 19:00 Perú (UTC-5).
12. **Caché #10/#11, funnel y last-error del trigger viven en el mismo KV** (`CHAT_LIMITS`). No borrar el namespace a ciegas. HIT de chat **ignora** `history[]`. Claves `funnel:` y `pipeline-trigger:last-error` **sin TTL**.
13. **`--gc` apagado:** chunks de culminados siguen en HNSW. Encenderlo borra vectores; no es un no-op.
14. **Anon key en el JS público** es a propósito (RLS lectura). Service role **nunca** al browser. El Worker Gemini **sí** tiene `SUPABASE_SERVICE_KEY` (`cotizar_tipo_log` + `/admin/stats`).
15. **No commitear** `.env`, `.dev.vars`, evals JSON, HTML Penpot (ya en `.gitignore` del monitor).
16. **`/cotizar` gasta 0 o 1 Flash:** HIT=0; MISS=1 generate (self-routing). Ya no hay clasificador Flash ni Δ2. El streaming de la iteración 7 **no** es token-a-token del modelo. No reintroducir `clasificarPorReglas` / `clasificarIntentFlash`.
17. **Query de #11:** `trim` + lowercase + colapsar espacios. **No** se quitan tildes (`dónde` ≠ `donde`). Hash SHA-256 de esa forma.
18. **Ruta default = postulables** (`esPostulable`). El chip «En evaluación / cerrados» muestra el resto. Dashboard usa la misma regla. B1 confirmó que el filtro no oculta; no “arreglarlo” para mostrar más mercado.
19. **`v_kpis_dashboard` puede timeout** (57014). El Dashboard no se cae: `cargarCapaSemantica` usa TS. No «arreglar» dropeando las vistas.
20. **502 `/analizar` ya no es JSON crudo**, pero el cupo ANALYZE se cobra igual. Funnel **no** se marca en 502. No reordenar el cupo sin pedido (es deliberado).
21. **`GET /funnel-pendientes` no es del front.** Auth `FUNNEL_TOKEN`. `GET /admin/stats` **sí** es del SPA admin: JWT de sesión, **no** el token del funnel.
22. **Flags `analizado`/`cotizado` son acumulativos.** TRUE no vuelve a FALSE. Marca cotizado **independiente** de `esCacheable`. HIT también marca. 409/502 no marcan cotizado.
23. **Vistas de conversión no están en `capa_semantica.sql`.** `docs/vista_kpis_conversion.sql`. `fmtTasa(null)` → "—", nunca 0%.
24. **Router Gemini:** GET = `/funnel-pendientes` **y** `/admin/stats`. El resto POST. CORS ya permite GET.
25. **Chat RAG ≠ internet.** El botón «Buscar en TDRs relacionados» no busca la web. No implementar RAG in-panel en #11 (encapsulamiento deliberado).
26. **CRITERIOS §3 «la IA busca» no está en código.** No inventar una tabla de precios “para cumplir el criterio” sin dimensionar B15.
27. **B20 no está cerrado al 100 %.** Un run `workflow_dispatch` (33319218551) prueba el mecanismo, no la serie de horarios. No diseñar B13/B14 encima todavía.
28. **`GITHUB_PAT` / `TRIGGER_TEST_TOKEN`:** los carga Rolando. Cursor no hace `wrangler secret put` de esos valores.
29. **`categoria_it` es cascada, no un job único.** Keywords en la ingesta; `reclasificar_categoria.py` para nulls viejos; Gemini **solo** si ambas columnas siguen NULL. El incremental **no** re-etiqueta. Keywords **no** leen `tdr_texto` / `items_json` / `nom_area_usuaria`.
30. **`clasificar_gemini.py` es herramienta manual.** `temperature: 0` no lo vuelve determinista: dry-run vigentes → 1; escritura → 3. Revisar el SELECT a mano. No descuenta cupos KV ni `flash_ocr_cuota.json`. Clasifica por objeto, no por área. `ninguna` → NULL. Toner / CPU de grupo electrógeno ≠ Hardware (FP 90592/90386 revertidos). **No** meterlo en el cron sin Arquitectura C.
31. **OCR `--solo-ti` exige etiqueta previa** (`es_ti` = `categoria_it` OR `relevancia_ia`). Huevo-gallina: un IT que keywords no vio nunca entra a Flash OCR. Arquitectura C es el lugar para romper ese ciclo, no un cron del script actual.
32. **Admin JWT no UPDATE `categoria_it`.** RLS de `contratos` es SELECT. Correcciones a mano: service role / SQL Editor.
33. **`categoria_it` y `relevancia_ia` son ejes independientes.** 13 categorías TI; solo IA/analytics es «tiene IA». Ruta pide **cualquiera** de las dos. No clasificar preguntando solo «¿tiene IA?».
34. **Windows cp1252:** prints con `→` u otro no-ASCII revientan el pipeline. ASCII (`->`) o UTF-8 forzado en stdout. Fix: `24a2a3b`.

---

## 8. Referencia rápida

| Qué | Valor |
|---|---|
| Front | https://seace.rdiaz-lab.xyz |
| Worker Gemini | https://seace-ai-proxy.rdiazg14.workers.dev |
| Trigger pipeline | https://seace-pipeline-trigger.rdiazg14.workers.dev |
| SEACE origen | https://prod6.seace.gob.pe/buscador-publico/contrataciones |
| Supabase | `wusywwhcyqngnpvpzxyr` · `https://wusywwhcyqngnpvpzxyr.supabase.co` |
| Monitor local | `d:\ROLANDO\DEV_APPS\seace8uit\seace-monitor` |
| Web local | `d:\ROLANDO\DEV_APPS\seace8uit\seace-web` |
| Worker local | `d:\ROLANDO\DEV_APPS\seace8uit\seace-ai-proxy` |
| Trigger local | `d:\ROLANDO\DEV_APPS\seace8uit\seace-pipeline-trigger` |
| HEAD monitor | `d430272` |
| HEAD web | `8a0b596` |
| HEAD worker | `da3caf8` |
| Worker CF Gemini | `cbf31b49-e3e7-44b0-a8cf-6cd4f5113ad4` |
| Pages | push `main` → Actions GitHub Pages (HEAD `8a0b596`) |
| Disparo pipeline | CF Cron `0 14 * * *` → `workflow_dispatch` (GHA `schedule:` respaldo) |
| Chat local | `npm run dev` (5173) |
| Worker local | `npx wrangler dev` |
| Deploy Worker | `npx wrangler deploy` **antes** de Pages |
| Deploy web | push `main` (Actions Pages) |
| Pipeline | CF trigger, o `workflow_dispatch` a mano; no confiar solo en `schedule:` |
| KV cupos + caché + funnel + last-error | namespace `c63cfd497041477f91dafdde5935f37d` (único, 2 Workers) |
| RPM | `CHAT_RPM` 8/60, namespace_id `8701` |
| Flash chat | `flash:{UTC-day}` cap 200 Δ2 |
| Flash cotizar | `cotizar:{UTC-day}` cap 80 **Δ1** |
| Docs | `TRASPASO_MAESTRO_SEACE.md` · `ARQUITECTURA_TECNICA.md` · `ESTADO_CIERRE_2026-08-29.md` · `CHANGELOG_ITERACIONES.md` · `CRITERIOS_DECISION_ENERTRONIC.md` |

Endpoints Worker Gemini: `POST /` · `POST /analizar` · `POST /cotizar` · `GET /funnel-pendientes` (FUNNEL_TOKEN) · `GET /admin/stats` (JWT admin) · `POST /embed` → 410.

Fecha snapshot: **1 sep 2026** (Perú).
