# Traspaso maestro — SEACE Monitor

Contexto completo para retomar el proyecto **sin chat previo**.
Snapshot: **20 ago 2026** (Perú), noche. Un solo punto de entrada; el detalle vive en los docs enlazados.

| Doc | Para qué |
|---|---|
| [ARQUITECTURA_TECNICA.md](./ARQUITECTURA_TECNICA.md) | Cómo/por qué de cada pieza, con `archivo:línea` y etiquetas MEDIDA / HEREDADA / POR-DEFECTO |
| [ESTADO_CIERRE_2026-08-20.md](./ESTADO_CIERRE_2026-08-20.md) | Foto de cierre: qué está en prod, cupos, backlog vivo |
| [CHANGELOG_ITERACIONES.md](./CHANGELOG_ITERACIONES.md) | Iteraciones 1–9 + fixes: qué, archivos, commit, estado |
| [CRITERIOS_DECISION_ENERTRONIC.md](./CRITERIOS_DECISION_ENERTRONIC.md) | Inteligencia de negocio (fuente de verdad del scoring) |
| [PLAN_DE_TRABAJO.md](./PLAN_DE_TRABAJO.md) | Plan v2 **parcialmente desactualizado** — ver §0 y §7 |

**Regla:** el código gana al PLAN. Secretos solo por **nombre** (nunca JWT, API keys ni tokens).

Este archivo + los enlaces de la tabla bastan para retomar. `DOCUMENTACION.md` y el README raíz del monitor describen el prototipo viejo (scrape 06:00, Llama); **no** empieces por ahí.

---

## 0. Cómo usar este documento

### Qué es SEACE Monitor (3 frases)

Sistema que deja de ser un buscador del SEACE y pasa a ser **asesor de licitaciones menores (≤ 8 UIT, techo S/42 800 en 2026)** para **ENERTRONIC** (TI peruana: IA, cloud, desarrollo, telemetría). Ingesta diaria el corpus público, rankea oportunidades sin IA (#9), analiza un TDR con Gemini incluyendo razonamiento de 2º orden (#10) y recálcula escenarios sobre ese análisis congelado (#11, panel + streaming). El humano pone el número final; la IA no oculta contratos ni cifra seca.

### Los 3 subrepos

Ruta local base: `d:\ROLANDO\DEV_APPS\seace8uit\`

| Repo | Qué hace | Prod |
|---|---|---|
| `seace-monitor` | Pipeline Python: ingesta SEACE → G1 estados → PDF/OCR → chunk → embed Gemini. SQL, evals, docs. | Cron GitHub Actions 09:00 Perú |
| `seace-web` | SPA autenticada (Ruta del día, análisis, chat, login) | https://seace.rdiaz-lab.xyz |
| `seace-ai-proxy` | Worker: RAG v2, `/analizar`, `/cotizar`, cupos | https://seace-ai-proxy.rdiazg14.workers.dev |

El front **no** llama a Gemini. Lee Supabase (anon + RLS) y pega al Worker. El pipeline **escribe** con service role. El Worker **lee** con anon.

### Método de trabajo

- **Expand-contract (D5):** columnas/RPC nuevas en paralelo; no dropear v1 hasta Fase 7. El RAG nunca queda a medias.
- **Probar chico** (dry-run, `--limit`, un contrato) → medir → recién entonces cron/prod.
- **Medir, no adivinar:** eval G4 (`data/eval_v2.json`, success@10 **0.633**). Etiquetas en arquitectura: MEDIDA vs POR-DEFECTO.
- **Puntos de control:** Rolando aprueba antes de cron, deploy masivo u OCR caro.
- **Calibración vs código:** el asistente (Rolando) define criterio de negocio; Cursor implementa. Criterios ENERTRONIC mandan sobre ocurrencias del LLM.

Al retomar: leé §6 (estado) + §7 (gotchas) + [CHANGELOG_ITERACIONES.md](./CHANGELOG_ITERACIONES.md) + lista POR-DEFECTO. No reabras retrieval v2 ni Auth salvo pedido explícito.

---

## 1. Mapa de los 3 repos

Hashes de este corte (HEAD `origin/main`):

| Repo | GitHub | Visibilidad | Rama | HEAD |
|---|---|---|---|---|
| seace-monitor | https://github.com/rdiazg14/seace-monitor | **público** | `main` | este commit de docs (padre `21921ef` capa SQL) |
| seace-web | https://github.com/rdiazg14/seace-web | **público** | `main` | `c0beff4` (Pages del mismo SHA) |
| seace-ai-proxy | https://github.com/rdiazg14/seace-ai-proxy | **privado** | `main` | `c8113ae` · Worker CF `075d03be-a84c-44eb-957f-7cca64bb6584` |

### 1.1 seace-monitor

**Stack:** Python 3.12, Playwright, supabase-py, httpx, PyMuPDF, pydantic. GitHub Actions. Local: `.env` (no versionado) con `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `GEMINI_API_KEY`. Scripts documentan `uv run python …`; el cron usa `pip install -r requirements.txt`. No hay `pyproject.toml`.

**Carpetas / archivos clave**

| Path | Rol |
|---|---|
| `ingesta_completa.py` | Altas incrementales al corpus |
| `refresh_estados.py` | G1 frescura de estado (`--gc` existe, **cron no lo pasa**) |
| `enriquecer_detalle.py` | Área + ítems CUBSO |
| `descargar_requerimiento.py` | PDF nativo + OCR selectivo |
| `chunker_contratos.py` | Chunks 800/500, sin overlap |
| `generar_embeddings.py` | Solo `embedding_v2`, `RETRIEVAL_DOCUMENT` + L2 |
| `alerta_g3.py` | Issue si un paso del job falla |
| `eval_retrieval.py` | G4 híbrido **pre-rerank** |
| `buscar_tdr_v2.sql` | RPC vector 1536 |
| `.github/workflows/pipeline.yml` | Cron diario **activo** |
| `docs/` | Traspaso, arquitectura, cierre 20-ago, changelog iteraciones, criterios, PLAN |
| `capa_semantica.sql` | Vistas Dashboard (iter. 9). Ya aplicado en Supabase. |

**Correr local (ejemplos reales de los scripts):**

```bash
uv run python refresh_estados.py --dry-run --limit 30
uv run python descargar_requerimiento.py --solo-nativo --limit 5
uv run python chunker_contratos.py --solo-pdf --solo-nuevos
uv run python generar_embeddings.py --backend gemini --ids 87164 --fuente pdf
uv run python eval_retrieval.py --backend v2
```

**Deploy:** no hay servidor. Push a `main` no despliega el pipeline; el cron `0 14 * * *` (09:00 Lima) corre `pipeline.yml`. `workflow_dispatch` permite OCR/skip/dry-run.

Workflows: `pipeline.yml` (activo, cron + manual) · `embed_v2.yml` / `pdf.yml` (solo manual, one-shot) · `scrape.yml` **DEPRECATED** (solo CSV token / prueba G3). El README raíz del repo **sigue describiendo scrape.yml como diario 06:00** — ignorarlo.

### 1.2 seace-web

**Stack:** React 19 + Vite 8 + TypeScript + Tailwind 3 + react-router 7 + supabase-js. Host: **GitHub Pages** (no Cloudflare Pages), dominio `seace.rdiaz-lab.xyz`.

**`src/` clave:** `App.tsx` rutas · `lib/supabase.ts` URL + `AI_PROXY` · `lib/rutaDia.ts` score #9 + `esPostulable` · `lib/capaSemantica.ts` Dashboard · `lib/analisis.ts` tipos #10/#11 · `pages/RutaDia.tsx` · `AnalisisContrato.tsx` (página #10 + panel #11) · `Dashboard.tsx` · `components/AnalisisV2.tsx` · `TimelineFishbone.tsx` · `ChatTable.tsx` / `ChatChart.tsx` · `Pills.tsx` (`CatItIaPill`) · `Chat.tsx` (RAG) · `Login.tsx` · `RequireAuth.tsx` · `supabase/perfiles.sql` + Edge Functions `crear-usuario` / `desactivar-usuario`.

**Local:**

```bash
npm install
npm run dev          # Vite, puerto 5173 (CORS del Worker lo permite)
```

`npm run build` → `tsc -b && vite build`. El workflow copia `dist/index.html` a `dist/404.html` (SPA en Pages).

**Deploy:** push a `main` → `.github/workflows/deploy.yml` (`Deploy seace-web → GitHub Pages`). HEAD de este corte: `c0beff4` (run `32441541562` success).

### 1.3 seace-ai-proxy

**Stack:** Cloudflare Workers, TypeScript, Wrangler 4. Bindings: `AI`, KV `CHAT_LIMITS`, rate limit `CHAT_RPM`.

**`src/`:** `index.ts` (router + RAG) · `analizar.ts` · `cotizar.ts` · `escenario.ts` · `history.ts` · `limits.ts`. Config: `wrangler.toml`. Secrets en CF, no en git: `.dev.vars` local (gitignored).

**Local:**

```bash
npm install
npx wrangler dev     # secrets desde .dev.vars
```

**Deploy:**

```bash
npx wrangler deploy
```

No hay Action de deploy del Worker: es manual (`npx wrangler deploy` desde esta carpeta). Versión viva: `npx wrangler deployments list` → este corte **`075d03be-a84c-44eb-957f-7cca64bb6584`** = git `c8113ae`.

Router (`index.ts`): solo **POST**. `/embed` → 410 · `/analizar` · `/cotizar` (JSON o SSE) · resto = chat RAG (`query` + `history?`). SSE si `Accept: text/event-stream`.

### Orden obligatorio: Worker **antes** que Pages

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
| `contratos` | Ficha SEACE. PK = `id` SEACE. IT: `categoria_it`, `relevancia_ia`. Fechas de cotización. Extra: `tdr_texto`, `pdf_hash`, `pdf_descargado`, `req_url` (`sin_pdf` = sin anexo PDF), `tdr_tipo_extraccion`, `paginas_ocr_*`, `estado_verificado_at`, `nom_area_usuaria`, `items_json` |
| `chunks_tdr` | `texto`, `embedding vector(768)` **v1**, `embedding_v2 vector(1536)`, `fuente` api\|pdf, `meta_entidad` / `meta_nro` |
| `perfiles` | `rol` admin\|normal, FK `auth.users` (`seace-web/supabase/perfiles.sql`) |
| `ingesta_rechazados` | G2 dead-letter |

**RPC:** `buscar_tdr_v2` (1536, default min_sim 0.20) · `buscar_contratos` (FTS) · `buscar_tdr` (768, **no** lo usa el chat v2).

**RLS:** `contratos` SELECT `anon`+`authenticated` (datos públicos SEACE). `chunks_tdr` SELECT abierto. Escritura: service role del pipeline. `perfiles`: propio o admin (`es_admin()`).

**Edge Functions** (verify JWT): `crear-usuario`, `desactivar-usuario`. Usan `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` en el entorno de Functions (`functions/_shared/admin.ts`). No están en git.

**Keys (nombres):** pipeline GitHub `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` · Worker secret `SUPABASE_ANON_KEY` · SPA: la anon key va **compilada en el JS público** (lectura RLS; no da escritura). Nunca commitear service_role.

### 2.2 Gemini (AI Studio)

| Uso | Modelo | Dónde |
|---|---|---|
| Embeddings | `gemini-embedding-001` @ **1536**, L2, QUERY vs DOCUMENT | Worker query RAG (1 por pregunta, **sin caché**); pipeline documentos (`batchEmbedContents`, lote 16) |
| Generación | `gemini-3.7-flash`, thinking `LOW` salvo clasificador `/cotizar` (temp 0, sin thinking) | Chat RAG (filtros + generate), `/analizar` (1× MISS), `/cotizar` (**1× o 2×**: reglas alta → solo generate; si no, clasificar + generate), OCR visión (cron) |

Secret: `GEMINI_API_KEY` — Cloudflare Worker **y** GitHub Actions (mismo nombre). No en `wrangler.toml`.

**Tope de gasto:** S/10/mes operado en **AI Studio** (no hay código que lo corte). Complementa los cupos del Worker. Monitoreo: consola AI Studio + KV `flash:` / `analyze:` / `cotizar:`.

Tier exacto de billing: **[por confirmar con Rolando]** (en sprints se habló de billing activo vs free; el backstop que manda es el S/10).

### 2.3 Cloudflare

- Account id (toml): `5a2b884f36bd62011960b879c3737546`
- Worker name: `seace-ai-proxy`
- Workers AI: **solo** reranker `@cf/baai/bge-reranker-base` (no embed BGE; `/embed` = 410)
- KV `CHAT_LIMITS` id `c63cfd497041477f91dafdde5935f37d` — **único** KV: cupos, caché `analyze:{id}:{hash}` **y** caché `chat:{id}:{hash}:{sha256}`.
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

**Secrets (solo nombre):** `GEMINI_API_KEY`, `SUPABASE_ANON_KEY`.

CORS orígenes: `https://seace.rdiaz-lab.xyz`, `https://rdiazg14.github.io`, `http://localhost:5173` (`index.ts` L87–91).

### 2.4 GitHub Actions

| Workflow | Cuándo | Secrets (nombre) |
|---|---|---|
| `pipeline.yml` | cron `0 14 * * *` + dispatch | `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `GEMINI_API_KEY` (+ `GITHUB_TOKEN` G3) |
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
- **Flujo:** ficha (`tdr_texto` / `pdf_hash`) → TDR columna o chunks (máx 80) → si chars &lt; **200** → **422** `sin_tdr` (sin cupo) → KV `analyze:{id}:{hash}` TTL 3 d (HIT = 0 Gemini) → cupos ANALYZE → 1 Flash JSON → post-proceso (`completarMomentoDia`, `asegurarRecomendada`, `alinearEconomiaConAlternativa`) → KV.put. Techo S/42 800.  
- Schema: 5 secciones base + `timeline.hitos`, `viabilidad.{ratio_alcance, cotizacion_por_componente, contradicciones_tdr}`, `alternativas[]`, `estructura_contractual`, `componentes_servicio`, `requisitos_proveedor`, `riesgos_contractuales`, `chips_sugeridos`, consorcio tri-estado.  
- 502: cuerpo `analisis_fallido` + banner amable + Reintentar. Cupo **sí** se gastó. Análisis **congelado**.

### #11 Cotización asistida

- **UI:** panel lateral 380 px en `/analisis/:id` (`ChatEscenarios`, `key={contratoId}`). Desktop ≥1024 px **abierto** al cargar; móvil cerrado + FAB. Persistencia `localStorage chat_escenarios_{id}`.  
- **Worker:** `POST /cotizar` `{ contrato_id, query, history? }` (SSE si `Accept: text/event-stream`)  
- **Flujo:** misma clave KV #10 → si no hay caché **409** `sin_analisis` (sin cupo) → caché exacta `chat:{id}:{pdf_hash}:{sha256}` (HIT = 0 Gemini, solo RPM; ignora history) → si MISS: reglas confianza alta (1 Flash) o clasificador Flash + generate (Δ2) → post-proceso → `KV.put` solo si `esCacheable` (sin supuestos ni montos) → SSE de presentación o JSON. Headers `X-Cotizar-Cache`, `X-Cotizar-Intent`. Fail-closed: sin `supuestos_aplicados[]` las cifras salen `null`. **No** reescribe el análisis. **No** RAG.  
- Por qué no reusar el chat RAG: buscaría **otros** contratos y gastaría Δ2 Flash de `flash:`. Ver arquitectura §F.

### Chat RAG + memoria #3

- **UI:** `/chat`  
- **Worker:** `POST /` `{ query, history? }` (opcional SSE)  
- Front manda últimos 4 pares, máx 8 ítems × 500 chars (`Chat.tsx` L118–131). Worker `sanitizeHistory`; `historyBlock` antepone `Conversación reciente:` **después** del retrieve (`history.ts` L30–35).  
- **Limitación:** embed/filtros/RAG ven **solo la query actual**. «¿y el plazo?» sí; «dame más de **ESE** contrato» puede recuperar otro. Reescribir query con Flash **no está en v1**.

### Páginas legacy (candidatas a jubilar)

| Ruta | Estado |
|---|---|
| `/` Dashboard | Iter. 9: lee `v_kpis_*` / `v_contratos_estado` (fallback TS). Filtros de urgencia **sin** leak de vencidos. Sigue siendo el **home**. |
| `/buscar` | FTS + chips. Un badge `CatItIaPill` (IT o IA, no ambos). |
| `/docs` | Notas de API. |
| `/usuarios` | Solo admin. |

Cuando Ruta del día sea el home, Dashboard/Buscador se pueden apagar. No bloquea.

---

## 4. Retrieval / RAG y pipeline

Detalle, números y «por qué»: [ARQUITECTURA_TECNICA.md](./ARQUITECTURA_TECNICA.md) §§A–C.

**RAG v2 (prod `RAG_BACKEND=v2`):** extraer filtros (1 Flash) → embed query 1536 L2 → paralelo `buscar_tdr_v2` (20, umbral 0.20) + FTS (20) → RRF k=60 a **nivel contrato** → ≤2 chunks/contrato → rerank `-base` top 5 → Flash. Eval G4: **27% → 63% success@10 sin reranker**.

**Pipeline diario (09:00 Perú):** ingesta → G1 **sin `--gc`** → detalle → PDF `--solo-nativo` → OCR `--solo-ti` 2 h / rpm 6 → chunk api + pdf `--solo-nuevos` → embed Gemini `WHERE embedding_v2 IS NULL` → G3. Escala ancla (~76k contratos, ~2 083 vigentes, ~9 169 chunks v2): si crece 10×, reabrir índice/umbral/chunk/OCR/FLASH_RPD.

---

## 5. Control de gasto (no romper)

Tres sistemas **aislados** (un flujo no descuenta al otro). Periodo RPD = **UTC**. RPM = binding atómico; RPD = KV get+put (no transacción).

| Sistema | RPM key | RPD IP | Global | Gemini |
|---|---|---|---|---|
| Chat | `ip:{ip}` 8/60 | `ip:{ip}:{day}` 40 | `flash:{day}` **200 Δ2** (extract+generate; embed **no** cuenta en Δ2) | |
| `/analizar` | `analyze:ip:{ip}` | `analyze:ip:{ip}:{day}` **15** | `analyze:{day}` **40** | 1 generate si MISS; HIT/422 = 0 |
| `/cotizar` | `cotizar:ip:{ip}` | `cotizar:ip:{ip}:{day}` **20** (1 pregunta) | `cotizar:{day}` **80 Δ1 o Δ2** | HIT=0 Gemini; reglas=1; Flash clasif=2; 409=0 |

**Qué gasta Gemini además:** OCR del cron (rpm 6, tope 2 h, `--max-ocr-dia 6000`); embeddings del pipeline (no pasan por `flash:`).

**Backstop:** S/10/mes AI Studio. Si el Worker no corta, la consola sí.

**502:** `/analizar` HTTP 502 `{ error: 'analisis_fallido', mensaje, reintentar, detalle_tecnico }` (banner en el front; visto históricamente contrato **66461** como JSON crudo). `/cotizar`: 502 JSON si falla antes del stream; si ya stremea, evento `error` (HTTP 200). Chat JSON: HTTP **200** con texto amable. Filtros v2: fallback regex, no 502. Cupo ANALYZE se cobra **antes** de Gemini. Detalle: arquitectura §G.

---

## 6. Estado actual y mejoras pendientes

### En producción y estable

Asesor #9 (postulables default) + #10 (2º orden) + #11 (panel, routing, streaming, **híbrido + caché exacta**) · capa semántica Dashboard · RAG v2 · memoria #3 · login · pipeline 09:00. Iteraciones **1–9 + fixes** en prod (tabla en `ESTADO_CIERRE_2026-08-20.md` y `CHANGELOG_ITERACIONES.md`). Calibración: **87880** SEDAPAR, **87502** CENEPRED.

No reabrir salvo pedido: cutover embed 768, Auth, cron horario, `RAG_BACKEND=v2`, schema 2º orden de #10, orden del cupo ANALYZE.

### Backlog (todo opcional)

| Ítem | Esfuerzo | Valor | Notas |
|---|---|---|---|
| Home = Ruta del día | Bajo | Enfoque | `/` = Dashboard (`App.tsx`). Navbar ya lista Ruta primero. |
| Aligerar `v_kpis_dashboard` | Bajo–medio | Estabilidad | SELECT \* puede timeout 57014; el front cae a TS |
| Caché semántica `/cotizar` | Medio | Tokens | Hoy solo exacta + `esCacheable` |
| No cobrar ANALYZE si 502 | Medio | Honestidad de cupo | Orden actual es deliberado (el 502 amable no lo tocó) |
| **#4 chunking** | Bajo–medio (eval offline, **no** prod de entrada) | Alto: fruta baja POR-DEFECTO | Overlap + tamaño vs baseline **63%**. |
| **Fase 7** drop BGE 768 + ivfflat | Bajo (SQL) + smoke | Limpieza | Chat v2 no los usa. |
| **#12 brief diario** | Medio | Producto (criterios §5) | Mail/resumen top-N; no existe. |
| Chat que lea KPIs SQL | Medio | Asesor | Fuera de iter. 9 a propósito. |
| Reranker v2-m3 / umbral / RRF k | Medio (eval **post-rerank**) | Precisión | Lista POR-DEFECTO. |

**Punto de entrada para optimizar retrieval** (arquitectura, cierre): reranker `-base` · threshold **0.20** · RRF **k=60** · chunk **800/500 sin overlap** → #4 vs 63%.

---

## 7. Gotchas (un chat nuevo no debe malinterpretar)

1. **«Vigente» ≠ ventana de cotización abierta.** G1 copia `idEstadoContrato` del SEACE (2 Vigente / 3 En Evaluación / 4 Culminado). `esPostulable()` exige Vigente **y** ventana Lima. `rankingActivo` **no** recorta (deja vencidos y En Evaluación); el **chip** default sí. No «arreglar» G1 para cerrar por `fecha_fin`.
2. **422 `/analizar` = TDR &lt; 200 chars**, no `req_url=sin_pdf`. Un sin_pdf **con chunks API** se analiza (p. ej. 83729 → 200). 422 real: ficha sin texto y sin chunks (p. ej. id 42).
3. **Solo un Supabase en código:** `wusywwhcyqngnpvpzxyr`. Otro proyecto inactivo: no tocar. Ref: [por confirmar con Rolando].
4. **Restos v1 en el Worker:** constante Llama `@cf/meta/llama-3.3-70b-instruct-fp8-fast`, `retrieveContext` 768, `RAG_BACKEND` default **v1 si falta env** (`index.ts` L129–130). Prod fija `v2` en toml. No usar `wrangler rollback` a v1 sin pedido. Fase 7 limpia BD; el bundle v1 se puede borrar después.
5. **`POST /embed` = 410.** El pipeline **no** embebe por el Worker.
6. **Eval 63% no incluye reranker** (`eval_retrieval.py` L334–335). No citar 63% como calidad post-rerank.
7. **PLAN vs realidad:** reranker v2-m3, chunks 200–400+overlap, GC al cerrar, costo $0/mes, checklist Gemini vacío, Fase 2 PDF «blocker» — **desactualizado**. Lista en arquitectura «Discrepancias». El PLAN §8 checklist no refleja que Gemini ya está en Actions y CF.
8. **README de seace-monitor** describe scrape 06:00 y CSV token como el producto. El diario es `pipeline.yml` 09:00. Empezá por este traspaso, no por el README.
9. **README del Worker** sigue corto (habla de `/analizar` con «mismos cupos»): **falso**. Cupos ANALYZE/COTIZAR/FLASH están aislados. El código gana.
10. **Home ≠ Ruta del día.** `/` es Dashboard (`App.tsx`). Navbar pone «Ruta del día» primero; el logo SEACE apunta a `/`. `path="*"` redirige a `/`.
11. **RPD en UTC, ranking en día Lima.** Un cupo «diario» cambia a las 19:00 Perú (UTC-5).
12. **Caché #10 y #11 viven en el mismo KV** (`CHAT_LIMITS`): `analyze:{id}:{hash}` vs `chat:{id}:{hash}:{sha256}` vs contadores `analyze:{day}`. No borrar el namespace a ciegas. HIT de chat **ignora** `history[]` y **no** descuenta RPD (sí RPM).
13. **`--gc` apagado:** chunks de culminados siguen en HNSW. Encenderlo borra vectores; no es un no-op.
14. **Anon key en el JS público** es a propósito (RLS lectura). Service role **nunca** al browser ni al Worker.
15. **No commitear** `.env`, `.dev.vars`, evals JSON, HTML Penpot (ya en `.gitignore` del monitor).
16. **`/cotizar` gasta 0, 1 o 2 Flash:** HIT=0; reglas alta=1 generate; si no, clasificador+generate. El streaming de la iteración 7 **no** es token-a-token del modelo: el JSON se genera entero y el texto se emite por chunks.
17. **Query de #11:** `trim` + lowercase + colapsar espacios. **No** se quitan tildes (`dónde` ≠ `donde`). Hash SHA-256 de esa forma.
18. **Ruta default = postulables** (`esPostulable`). El chip «En evaluación / cerrados» muestra el resto. Dashboard usa la misma regla (SQL o fallback TS). No reabrir el ranking «nada se oculta» del corte de la mañana del 20 ago.
19. **`v_kpis_dashboard` puede timeout** (57014). El Dashboard no se cae: `cargarCapaSemantica` usa TS. No «arreglar» dropeando las vistas.
20. **502 `/analizar` ya no es JSON crudo**, pero el cupo ANALYZE se cobra igual. No reordenar el cupo sin pedido (es deliberado).

---

## 8. Referencia rápida

| Qué | Valor |
|---|---|
| Front | https://seace.rdiaz-lab.xyz |
| Worker | https://seace-ai-proxy.rdiazg14.workers.dev |
| SEACE origen | https://prod6.seace.gob.pe/buscador-publico/contrataciones |
| Supabase | `wusywwhcyqngnpvpzxyr` · `https://wusywwhcyqngnpvpzxyr.supabase.co` |
| Monitor local | `d:\ROLANDO\DEV_APPS\seace8uit\seace-monitor` |
| Web local | `d:\ROLANDO\DEV_APPS\seace8uit\seace-web` |
| Worker local | `d:\ROLANDO\DEV_APPS\seace8uit\seace-ai-proxy` |
| HEAD monitor | este commit de docs (padre `21921ef`) |
| HEAD web | `c0beff4` |
| HEAD worker | `c8113ae` |
| Worker CF | `075d03be-a84c-44eb-957f-7cca64bb6584` |
| Pages | push `main` → Actions GitHub Pages (último success: `c0beff4` / run `32441541562`) |
| Cron | `0 14 * * *` = 09:00 América/Lima |
| Chat local | `npm run dev` (5173) |
| Worker local | `npx wrangler dev` |
| Deploy Worker | `npx wrangler deploy` **antes** de Pages |
| Deploy web | push `main` (Actions Pages) |
| Pipeline | espera al cron o `workflow_dispatch` |
| KV cupos + caché #10 + #11 | namespace `c63cfd497041477f91dafdde5935f37d` (único) |
| RPM | `CHAT_RPM` 8/60, namespace_id `8701` |
| Flash chat | `flash:{UTC-day}` cap 200 Δ2 |
| Flash cotizar | `cotizar:{UTC-day}` cap 80 Δ1 o Δ2 |
| Docs | `TRASPASO_MAESTRO_SEACE.md` · `ARQUITECTURA_TECNICA.md` · `ESTADO_CIERRE_2026-08-20.md` · `CHANGELOG_ITERACIONES.md` · `CRITERIOS_DECISION_ENERTRONIC.md` |

Endpoints Worker: `POST /` · `POST /analizar` · `POST /cotizar` · `POST /embed` → 410.

Fecha snapshot: **20 ago 2026** (Perú).
