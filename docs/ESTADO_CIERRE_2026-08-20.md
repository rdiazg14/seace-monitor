# Estado de cierre — 20 ago 2026

Foto para retomar **sin chat previo**. Cómo/por qué: [ARQUITECTURA_TECNICA.md](./ARQUITECTURA_TECNICA.md). Historia de sprints: [CHANGELOG_ITERACIONES.md](./CHANGELOG_ITERACIONES.md). Punto de entrada: [TRASPASO_MAESTRO_SEACE.md](./TRASPASO_MAESTRO_SEACE.md).

Corte: **20 ago 2026** (Perú), noche (~21:55). Iteraciones **1–9 + fixes** están en producción. Nada de eso quedó a medias en git.

---

## PASO 0 — Tareas recientes (estado real)

| Tarea | Git | Deploy | Estado |
|---|---|---|---|
| **Iteración 8** — clasificador híbrido + caché exacto `/cotizar` | Worker `ecc186a` (luego `c8113ae` encima) | Worker CF `075d03be-a84c-44eb-957f-7cca64bb6584` | **HECHO en prod** |
| **Iteración 9** — capa semántica + Dashboard | Monitor `21921ef` (`capa_semantica.sql`); web `b366060` | SQL aplicado en Supabase `wusywwhcyqngnpvpzxyr`; Pages OK | **HECHO en prod**. Matiz: `v_kpis_dashboard` a veces timeout (57014); el Dashboard cae a fallback TS con las **mismas** reglas `esPostulable` / `clasificarNivel`. `v_contratos_estado` y `v_kpis_negocio` responden. |
| **Fix Buscador** — un solo badge IT/IA | Web `6ba2eeb` | Pages OK | **HECHO en prod** |
| **Fix 502 amable `/analizar`** | Worker `c8113ae`; web `c0beff4` | Worker CF `075d03be…`; Pages OK | **HECHO en prod**. Cupo ANALYZE **sigue** cobrándose antes de Gemini (no era parte del fix). |
| Fix postulabilidad Ruta del día | Web `cffcc2b` | Pages OK | **HECHO en prod** (anterior a 8/9; documentado aquí porque los docs viejos aún lo listaban como pendiente) |

No hay rama local divergente: los tres `main` están alineados con `origin/main` en este corte (monitor: este commit de docs encima de `21921ef`).

---

## Qué está en producción

| Pieza | URL | Commit / versión viva |
|---|---|---|
| Front (GitHub Pages) | https://seace.rdiaz-lab.xyz | `c0beff4` (502 amable). Actions `Deploy seace-web → GitHub Pages` run `32441541562` **success** (21 ago 02:55 UTC = 20 ago 21:55 Perú) |
| Worker | https://seace-ai-proxy.rdiazg14.workers.dev | git `c8113ae` · CF **`075d03be-a84c-44eb-957f-7cca64bb6584`** (`npx wrangler deployments list`, 21 ago 02:55 UTC) |
| Pipeline | cron 09:00 Perú (`0 14 * * *`) | monitor `dcf0a29` (pipeline 20 ago: corpus=**76 509**, nuevos=95, OCR=completo) + SQL capa `21921ef` |
| Supabase | `wusywwhcyqngnpvpzxyr` | Vistas `v_contratos_estado`, `v_kpis_dashboard`, `v_kpis_negocio` + `fn_rubro_energetic` **aplicadas** |

Flujos vivos:

- **#9 Ruta del día** — `/ruta-dia`, score 0–100 sin IA. Definición única `esPostulable()`: `estado==='Vigente'` y (`fecha_fin` null o `>= hoy` Lima). Ranking default = postulables; chip «En evaluación / cerrados».
- **#10 Análisis** — `/analisis/:id` → `POST /analizar`. Caché KV 3 d (`analyze:{id}:{pdf_hash}`). Schema 2º orden (ver arquitectura §E). 422 `sin_tdr` si TDR &lt; 200 chars. 502 estructurado `analisis_fallido` + banner amable + Reintentar.
- **#11 Cotización** — panel lateral → `POST /cotizar`. Lee caché #10; 409 si no hay análisis. Clasificador **híbrido** (reglas confianza alta **o** Flash) + generate. Caché exacta `chat:{id}:{pdf_hash}:{sha256}` si `esCacheable`. SSE de presentación. Fail-closed sin `supuestos_aplicados`.
- **Chat RAG v2** — `POST /` (`backend=v2`). SSE del modelo.
- **Dashboard** — `/` lee capa semántica (SQL, fallback TS). Filtros de urgencia **no** meten vencidos en «hoy/semana».
- **Login** — `signInWithPassword`; signup público cerrado.
- **Pipeline diario** — ingesta → G1 (sin `--gc`) → detalle → PDF nativo → OCR selectivo 2 h → chunk → embed `embedding_v2`.

---

## Producto asesor (iteraciones 1–9 + fixes)

Cada fila es capacidad **en prod**. Commits ancla; el Worker vivo es siempre el HEAD (`c8113ae`).

| # | Capacidad | Endpoint / UI | Archivos | Contrato |
|---|---|---|---|---|
| 1 | Razonamiento 2º orden | `POST /analizar` | `analizar.ts` | `timeline.hitos[]`; `viabilidad.ratio_alcance` + `cotizacion_por_componente[]` + `contradicciones_tdr[]`; `alternativas[]` (1 `recomendada`); más `estructura_contractual`, `componentes_servicio`, `requisitos_proveedor`, `riesgos_contractuales`, `chips_sugeridos` |
| 2 | UI estructural | `/analisis/:id` | `AnalisisV2.tsx`, `AnalisisContrato.tsx` | Infografía, N alternativas, economía por componente, contradicciones |
| 2.5 | Coherencia + consorcio | `/analizar` + UI | `alinearEconomiaConAlternativa` | `economia` = vía recomendada; consorcio tri-estado |
| 3 | Timeline fishbone | `/analisis/:id` | `TimelineFishbone.tsx` | Thumbnail + fullscreen |
| 4 | Routing chat | `POST /cotizar` | `cotizar.ts`, `escenario.ts`, `ChatTable`, `ChatChart` | Schema condicional texto/tabla/grafica/tabla_grafica |
| 5 | Panel atado al contrato | `/analisis/:id` | `AnalisisContrato.tsx` | `key={contratoId}`; `chat_escenarios_{id}` |
| 6 | Pulido | `#10` + UI | `completarMomentoDia`; CSS charts | `momento_dia` siempre estimado |
| 7 | UX conversacional | `/cotizar` + panel | `cotizar.ts`, `AnalisisContrato.tsx` | Panel default ≥1024 px; SSE presentación |
| — | Postulabilidad #9 | `/ruta-dia` | `rutaDia.ts` `esPostulable` | Default solo postulables |
| 8 | Híbrido + caché chat | `POST /cotizar` | `cotizar.ts` | Reglas+Flash; `chat:…`; `esCacheable`; headers |
| 9 | Capa semántica | `/` Dashboard | `capa_semantica.sql`, `capaSemantica.ts`, `Dashboard.tsx` | Vistas + KPIs negocio; leak fechas corregido |
| — | Badge Buscador | `/buscar` | `CatItIaPill` | Un badge, no dos |
| — | 502 amable | `/analizar` | `analizar.ts`, `AnalisisContrato.tsx` | Cuerpo estructurado + banner; cupo **no** se reordena |

---

## Cómo se protege el gasto

Tres sistemas de cupos **aislados** (claves KV distintas; un flujo no descuenta al otro):

| Sistema | RPM | RPD IP | Global día | Gemini por request |
|---|---|---|---|---|
| Chat RAG | `ip:` 8/60 | CHAT_RPD=40 | `flash:` FLASH_RPD=200 **Δ2** | extract + generate (embed **no** entra en Δ2) |
| `/analizar` | `analyze:ip:` 8/60 | ANALYZE_IP_RPD=15 | `analyze:` ANALYZE_RPD=40 | 1 generate si MISS; HIT/422=0. **MISS que termina 502 igual cobró** el cupo (límites **antes** de Gemini) |
| `/cotizar` | `cotizar:ip:` 8/60 | COTIZAR_IP_RPD=20 (1 pregunta) | `cotizar:` COTIZAR_RPD=80 **Δ1 o Δ2** | HIT caché chat: **0** Gemini (solo RPM). MISS reglas alta: **1** generate. MISS sin reglas: clasificador + generate **Δ2**. 409=0 |

Tope de facturación Gemini: **S/10/mes en AI Studio** (no está en el código). Números: arquitectura §G.

Caché de contenido en el **mismo** KV `CHAT_LIMITS`:

| Clave | Qué | TTL |
|---|---|---|
| `analyze:{id}:{pdf_hash\|na}` | JSON #10 | 3 d |
| `chat:{id}:{pdf_hash}:{sha256(query normalizada)}` | JSON #11 si `esCacheable` | 3 d |

`esCacheable`: **no** cachea si hay `supuestos_aplicados` o cualquier monto estimado. La query se normaliza con `trim` + lowercase + colapsar espacios; **no** se quitan tildes.

Instrumentación diaria (UTC): `chat_cache:hit:{day}`, `chat_cache:miss:{day}`, `chat_rules:{day}`, `chat_flash_clasif:{day}`. Headers: `X-Cotizar-Cache: HIT|MISS`, `X-Cotizar-Intent: reglas|flash`, `X-Analisis-Cache: HIT|MISS`.

---

## Limitación conocida de #3 (chat RAG)

Seguimiento **conceptual** sí («¿y el plazo?»). Referencia que exige re-recuperar («dame más de ESE contrato») no: embed/filtros/RAG ven solo la query actual. Reescribir la query con Flash **no está**.

Caché #11 HIT **ignora** `history[]` (devuelve el escenario guardado).

---

## Backlog vivo (nada de esto bloquea el asesor)

| Ítem | Notas |
|---|---|
| Home = Ruta del día | `/` sigue siendo Dashboard (`App.tsx`) |
| **#4 chunking** | Overlap + tamaño vs baseline **63%** success@10 (eval **offline**) |
| **Fase 7** | Dropear `embedding(768)` + ivfflat; chat v2 no los usa |
| **#12 brief diario** | Mail/resumen top-N; no existe |
| Aligerar `v_kpis_dashboard` | SELECT \* puede timeout; el front ya tiene fallback TS |
| Caché semántica `/cotizar` | Hoy solo exacta + `esCacheable` |
| No cobrar ANALYZE si Gemini 502 | Orden del cupo **no** se cambió a propósito |
| Retry 1× `/analizar` | No está |
| Chat que lea KPIs SQL | Fuera de iter. 9 a propósito |
| Reranker v2-m3 / umbral / RRF k | Lista POR-DEFECTO |
| Encender `--gc` | Borra chunks de culminados; no es no-op |

**Punto de entrada para reevaluar retrieval:** reranker `bge-reranker-base` · threshold **0.20** · RRF **k=60** · chunk **800/500 sin overlap** → Tarea #4 vs 63%.

---

## Referencia rápida de HEADs (este corte)

| Repo | HEAD `origin/main` | Notas |
|---|---|---|
| seace-monitor | este commit de docs (padre `21921ef`) | público |
| seace-web | `c0beff4` | público · Pages del mismo SHA |
| seace-ai-proxy | `c8113ae` | **privado** · Worker CF `075d03be-a84c-44eb-957f-7cca64bb6584` |

Fecha snapshot: **20 ago 2026**.
