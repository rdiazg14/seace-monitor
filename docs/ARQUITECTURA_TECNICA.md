# Arquitectura técnica — SEACE Monitor

Documento de **cómo funciona hoy** cada pieza, con evidencia `archivo:línea`.
No sustituye al PLAN: si código y PLAN divergen, se anota aquí. Estado de producto asesor (iter. 1–7): también `ESTADO_CIERRE_2026-08.md`.

---

## Índice

0. Snapshot y repos  
0.5 Escala (ancla para reevaluar)  
A. Retrieval / RAG  
B. Chunking  
C. Pipeline de datos  
D. Scoring Ruta del día (#9)  
E. Análisis de contrato (#10)  
F. Cotización asistida (#11)  
G. Rate-limiting y gasto  
H. Frontend y datos  
Cierre: tabla de decisiones · discrepancias vs PLAN · commits

---

## 0. Snapshot y repos

| Pieza | Path | HEAD documentado |
|---|---|---|
| Pipeline / SQL / evals | `seace-monitor` | `e5f58d6` (backfill `chunk_embed_text` PDF) |
| Worker | `seace-ai-proxy` | `437c265` (`/cotizar` SSE + clasificador) · CF `49953466-a6dd-4878-b549-8ebb5567bbcc` |
| Front | `seace-web` | `9426dc1` (UX conversacional iter. 7) · Pages `https://seace.rdiaz-lab.xyz` |

Worker vivo: `https://seace-ai-proxy.rdiazg14.workers.dev`. Front: `AI_PROXY` = esa URL (`seace-web/src/lib/supabase.ts` L7).

Fecha de este corte: **20 ago 2026** (Perú). Asesor #10/#11 en prod con iteraciones 1–7; retrieval/pipeline sin cambio de diseño desde el corte del 18 ago.

Etiquetas de «por qué»:

- **MEDIDA** — hay número en eval/A-B/código o en un A/B corrido (p. ej. `eval_v2.json`).
- **HEREDADA** — está en PLAN/criterios o se implementó a propósito, **sin** re-medir en G4.
- **POR-DEFECTO** — valor típico o el que quedó en el archivo; no hay A-B.
- **POR-CONFIRMAR** — no consta en código ni eval; no se inventa.

---

## 0.5 Escala (ancla)

Números de producto en este corte (los dio Rolando para el doc; no se re-contaron aquí):

| Universo | Magnitud |
|---|---|
| Contratos en BD | ~76 000 (pipeline 18 ago: corpus=76 334) |
| Vigentes | ~2 083 |
| Chunks con `embedding_v2` | ~9 169 |
| PDF | 679 nativo / 102 mixto / 1 398 imagen / 150 sin_pdf |

Las decisiones de esta arquitectura se consideran **adecuadas bajo esta escala**. Si el corpus o los chunks vigentes crecen **~10×**, hay que reabrir al menos: HNSW vs otro índice, threshold 0.20, tamaño de chunk, RRF k, tope OCR 2 h, FLASH_RPD=200, y si el reranker `-base` aguanta.

---

## A. Retrieval / RAG

### Qué hace

El chat `POST /` (no `/analizar` ni `/cotizar`) arma contexto TDR y genera con Gemini Flash. Backend de prod: `RAG_BACKEND=v2` (`seace-ai-proxy/wrangler.toml` L13).

Pipeline v2, en orden (`retrieveContextV2`, `index.ts` L537–589):

1. `extraerFiltrosV2` — 1× Flash JSON (estado / término / entidad).  
2. `embedQueryGemini` — Gemini embed `RETRIEVAL_QUERY` 1536 + L2 del **término extraído** (`filtros.termino || query`, L540), no del hilo.  
3. En paralelo: RPC `buscar_tdr_v2` (20 chunks, umbral) + RPC `buscar_contratos` FTS (20).  
4. RRF a **nivel contrato** (no chunk), top 20.  
5. Hasta 2 chunks vectoriales por contrato (o el hit FTS si no hay vector).  
6. `rerankTop` Workers AI → 5 ítems.  
7. `geminiGenerate` — 1× Flash (SSE o JSON) con fragmentos + query (+ `history[]` opcional).

`reserveFlash(env, 2)` cuenta extract+generate (`index.ts` L839–841). El embed **no** entra en ese delta 2.

### Cómo (valores reales)

| Paso | Valor | Evidencia |
|---|---|---|
| Embed modelo | `gemini-embedding-001` | `index.ts` L95, L429–436 |
| Dims | 1536, recorte si viene de más | `GEMINI_DIM` L98, L444–445 |
| Query task | `RETRIEVAL_QUERY` | L436 |
| Documento (pipeline) | `RETRIEVAL_DOCUMENT` | `generar_embeddings.py` L244, L468 |
| Norma | L2 en query y en documentos | `index.ts` L152–158, L446; `generar_embeddings.py` `l2_normalize` |
| Índice vector | HNSW coseno sobre `embedding_v2` | `docs/schema_rag_v2.sql` L53–55; RPC `buscar_tdr_v2.sql` L4–5, L31–38 |
| Índice v1 (aún en BD) | ivfflat `embedding(768)` lists=100 | `schema_rag.sql` L33–36. **No** lo usa el chat con `RAG_BACKEND=v2` |
| Default código si falta env | `ragBackend` cae a **v1** | `index.ts` L129–130. Prod fija `RAG_BACKEND=v2` en wrangler L13 |
| Distancia RPC | `<=>` pgvector; similarity = `1 - dist` | `buscar_tdr_v2.sql` L31 |
| Umbral v2 | `0.20` (`SIMILARITY_THRESHOLD_V2`) | `wrangler.toml` L12; default código L143–146; SQL default L13 |
| Umbral v1 (apagado) | `0.70` | `wrangler.toml` L11 |
| RRF k | **60** | `RRF_K` `index.ts` L97, L478: `1/(RRF_K + i + 1)` con `i` 0-based |
| Reranker | `@cf/baai/bge-reranker-base`, top_k=5, texto ≤1500 | L94, L486–511 |
| Fallback rerank | si CF falla → primeros 5 del RRF | L509–510 |
| LLM | `GEMINI_FLASH_MODEL=gemini-3.7-flash`, thinking `LOW`, temp 0.2, max 2048 | `wrangler.toml` L14; `index.ts` L624–627 |
| Filtros | Flash JSON; si falla HTTP/parse → regex `extraerTermino` / `extraerEntidad`, estado default Vigente | L373–422 |
| History #3 | sanitizado máx 8×500; **después** del retrieve; embed/filtros ven la query actual (término), no el hilo | `history.ts` L9–13; `userPromptFrom` L592–597 |

`POST /embed` (BGE 768) responde **410** (`index.ts` L160–165).

Eval G4 **no incluye reranker**:

```
eval_retrieval.py L334–335: «reranker: no (solo Worker). Eval = híbrido pre-rerank.»
eval_baseline_v1.json: success@10 = 0.266… (BGE 768, min_sim 0.70, 30 queries, 12 sin hits)
eval_v2.json: success@10 = 0.633… (Gemini 1536 + buscar_tdr_v2+RRF, min_sim 0.20, 0 queries sin hits)
```

### Por qué

| Elección | Etiqueta | Qué sí consta |
|---|---|---|
| Gemini 1536 vs BGE 768 | **MEDIDA** | 26.7% → 63.3% success@10 en los JSON de `data/eval_*.json` (híbrido **pre-rerank**) |
| HNSW vs ivfflat v2 | **HEREDADA** | PLAN D3 (`PLAN_DE_TRABAJO.md` L43): ivfflat lists=100 mal para ~9k. **No** hay A-B HNSW vs ivfflat en `eval_v2.json` |
| 1536 dims | **HEREDADA** | PLAN D2 (tope pgvector ~2000). No hay eval 768-Gemini vs 1536 |
| RRF siempre (no fallback «si &lt;3 chunks») | **HEREDADA** | PLAN D6; código L541–572 siempre fusiona |
| RRF k=60 | **POR-DEFECTO** | Constante en código; no hay barrido de k |
| Threshold 0.20 | **POR-DEFECTO** | Default SQL + wrangler. G4 usó 0.20. No hay curva umbral |
| `bge-reranker-base` | **POR-DEFECTO** / discrepancia PLAN | PLAN pide `bge-reranker-v2-m3`. El Worker usa `-base`. G4 no lo midió |
| Flash 3.7 + thinking LOW | **POR-CONFIRMAR** | Está en wrangler; no hay A-B de modelo en evals |
| History no entra al embed | **HEREDADA** | Limitación anotada en `history.ts` L9–13: re-embed del hilo sería otra Flash |

### Alternativas y cuándo reconsiderar

- Reranker **v2-m3** (PLAN): cuando se arme un eval **post-rerank** (el 63% no lo cubre).  
- Threshold / k RRF / top-20: si sube el ruido o los vigentes 10×.  
- Reescribir query con historial (#3 v2): solo si «dame más de ESE contrato» duele en uso real.

---

## B. Chunking

### Qué hace

`chunker_contratos.py`: parte TDR/ficha en filas `chunks_tdr`. Cron: primero `fuente=api`, luego `--solo-pdf --solo-nuevos` (`pipeline.yml` L123–139).

### Cómo

| Parámetro | Hoy | Evidencia |
|---|---|---|
| Split si tokens ≳ | **800** (`len/4`) | L39, L147–148, L174–175 |
| Subchunk objetivo | **500** tokens | L40 |
| Overlap | **No** | `split_por_parrafos` L50–68: al llenar el buffer arranca otro con el párrafo actual, sin copiar cola del anterior |
| API header | Largo `[ENTIDAD \| asunto \| Nº]` en `texto` | docstring L5 |
| PDF header | Corto `[SIGLAS \| Nº]` en `texto` (display); embed **sin** esa línea | L5–7, `cuerpo_chunk` L117–121 |
| Embed PDF | `--embed-mode auto` (default) → pdf=body, api=header | `generar_embeddings.py` L90–93, L613–616 |
| Idempotencia PDF | `--solo-nuevos` no reescribe chunks pdf ya existentes | cron L139 |

A/B header vs body (contrato **87164**, 16 ago 2026, `probar_pdf_rag.py`): mean pairwise hermanos **header 0.8720 / body 0.8405**; cos(SGD, nepotismo) **0.9157 / 0.8930**. Ganó **body** (menos colapso). El «~0.93» del diagnóstico era el síntoma del header largo (mensaje de Rolando), no un archivo en `data/`.

### Por qué

| Elección | Etiqueta |
|---|---|
| Embeber cuerpo PDF (no header repetido) | **MEDIDA** (A/B 87164) |
| 800/500, sin overlap | **POR-DEFECTO** — PLAN Fase 4 pedía ~200–400 **con solape** (`PLAN_DE_TRABAJO.md` L174). Medición ya diseñada: Tarea #4 (overlap + tamaño, eval offline contra el baseline 63% success@10). |
| Header API largo intacto | **POR-CONFIRMAR** — el A/B fue sobre PDF |

### Alternativas

#4 (pendiente): overlap y/o tamaño 200–400, **solo eval offline** contra el 63%. Chunking semántico si 800/500 deja requisitos partidos. Reabrir si vigentes o PDF crecen 10×.

---

## C. Pipeline de datos

### Qué hace

Un job diario: altas → frescura de estado → detalle web → PDF nativo → OCR acotado → chunk → embed v2. **No** escribe `embedding(768)` ni llama `POST /embed`.

### Cómo

Orden (`pipeline.yml`):

| # | Paso | Comando | Notas |
|---|---|---|---|
| Cron | 09:00 Perú | `0 14 * * *` | L6–7 |
| 1 | Ingesta | `ingesta_completa.py` | incremental |
| 2 | G1 | `refresh_estados.py` | **sin** `--gc` |
| 3 | Detalle | `enriquecer_detalle.py` | `continue-on-error` |
| 4 | PDF nativo | `descargar_requerimiento.py --solo-nativo --limit 0` | PyMuPDF, 0 Flash |
| 5 | OCR | `--solo-ocr --solo-ti --max-segundos 7200 --rpm 6 --max-ocr-dia 6000` | step `timeout-minutes: 125` (L103); job entero 240 min (L42) |
| 6–7 | Chunk api + pdf delta | `chunker_contratos.py` / `--solo-pdf --solo-nuevos` | L123–139 |
| 8 | Embed | `generar_embeddings.py --backend gemini` | `WHERE embedding_v2 IS NULL` |
| 9 | G3 | `alerta_g3.py` por paso si falla + alerta final `always()` | L183–187 |

**G1** (`refresh_estados.py`): relee SEACE; UPSERT `{id, estado, estado_verificado_at}`. Vigentes todos cada corrida; En Evaluación por lotes. Terminal = `idEstadoContrato` **4** Culminado (L45–48). `--gc` borra chunks de cierres &gt;60 días (L12, L219) — **el cron no lo pasa**.

**G2:** inválidos → `ingesta_rechazados` (`ingesta_rechazados.sql`), no a `contratos`.

**OCR selectivo:** vigentes + **ventana de cotización abierta** (`ventana_cotizacion_abierta`, `descargar_requerimiento.py` L999–1000: `fecha_fin` NOT NULL y &gt; now) + `--solo-ti` (hace falta `categoria_it` o `relevancia_ia`). Por página: `paginas_ocr_pendientes` / `paginas_ocr_hechas` (no re-OCR). Reloj 2 h.

**Anti-reproceso:** `pdf_descargado=false` para bajar; embed solo NULL; OCR páginas hechas; chunk pdf `--solo-nuevos`.

### Por qué

| Elección | Etiqueta |
|---|---|
| Cutover solo `embedding_v2` | **HEREDADA** — expand-contract PLAN D5; cron L2 |
| OCR no masivo | **HEREDADA** — cupo Flash vs chat; `--solo-ti` + 2 h en el yaml |
| `--gc` apagado | **POR-CONFIRMAR** — el flag existe; no está en el workflow. PLAN G1 sí pedía borrar chunks al cerrar |
| G1 no usa `fecha_fin_cotizacion` para GC | **HEREDADA** — comentario en `refresh_estados.py` L166–172: esa fecha es ventana de cotización, no cierre del contrato. Terminal = `idEstadoContrato` 4 (L45–48, observado 2026-08-16 en el mismo archivo) |

### Alternativas

Encender `--gc` cuando se acepte borrar chunks de culminados. OCR más amplio si hay cuota. Reabrir 2 h / rpm 6 si la cola imagen (1 398) no baja.

---

## D. Scoring Ruta del día (#9)

### Qué hace

`/ruta-dia` rankea oportunidades **sin IA**, 0–100, desde columnas de `contratos`.

### Cómo

Fórmula (`rutaDia.ts` L4–12, L247–258):

`score = rubro(50) + vigencia(25) + urgencia(15) + señales(10)` (tope 100).

| Bloque | Puntos | Código |
|---|---|---|
| Rubro | Núcleo 50 / Adyacente 38 / Oportunista 24 / Marginal 12 | `PTS_RUBRO` L102–107 |
| Vigencia | Vigente 25 / En Evaluación 12 | L255 |
| Urgencia (solo Vigente, ventana no vencida) | hoy **10** · mañana **12** · **2–7 d = 15** · 8–30 d 8 · &gt;30 d 5 · sin fecha 3 · vencido 0 | `ptsUrgencia` L225–235 |
| Señales | ALTA+IA real 6 / MEDIA 3 / BAJA 1 · objeto Servicio +2 (máx 10) | L237–244 |

Mapeo `categoria_it` → nivel: L79–93. Overlay texto: telemetría/SCADA/OT/IoT → Núcleo; integración/automatización/digital twin → Adyacente; **nunca baja** (L215–216). Firma digital + ALTA **no** sube a Núcleo (`cat !== 'Firma digital'`, L218–219).

Ranking activo: En Evaluación entra; Vigente solo si `fecha_fin >= today` o sin fecha (`rankingActivo` L275–284).

### Por qué

| Elección | Etiqueta | Texto en código |
|---|---|---|
| Sin IA | **HEREDADA** | L2: «100% desde BD, sin IA». Fuente: `docs/CRITERIOS_DECISION_ENERTRONIC.md` |
| 2–7 d &gt; hoy | **HEREDADA** | L11: «cierra hoy es bandera, no dominancia» |
| Firma digital no sube | **HEREDADA** | L23: token cripto ≠ tokens de IA |
| Modalidad/pago/margen = 0 aquí | **HEREDADA** | L14: eso vive en #10 |

### Alternativas

Meter señales de #10 al ranking cuando se quiera (costo + sesgo del LLM). No hace falta para el flujo diario actual.

---

## E. Análisis (#10)

### Qué hace

`POST /analizar` `{ contrato_id }` → JSON estructurado para ENERTRONIC. UI: `/analisis/:id`. Una llamada Flash **solo en MISS** de caché.

El schema **required** sigue siendo las 5 secciones + `resumen` + `optimizacion` (`analizar.ts` L423). Encima, el system prompt exige razonamiento de 2º orden (campos opcionales en el schema JSON, obligatorios en instrucciones):

| Campo | Contenido |
|---|---|
| `timeline.hitos[]` | Secuencia variable: `orden`, `nombre`, `tipo`, `momento_texto`, `momento_dia` (siempre estimado), `tiene_pago`, `es_critico` |
| `viabilidad.ratio_alcance` | `valor_mercado_min/max`, `techo_contrato`, `ratio_texto`, `lectura` |
| `viabilidad.cotizacion_por_componente[]` | `componente`, `mercado_min/max` |
| `viabilidad.contradicciones_tdr[]` | inconsistencias internas; array vacío válido |
| `alternativas[]` | N variable (1 si la directa es viable; 2–3 si no). Exactamente una `recomendada=true`. Cada una con `economia.{valor,costo,margen}` |
| `requisitos_proveedor.admite_consorcio` | `true` solo si el TDR lo dice; **`null` si no consta** (no se infiere) |
| `chips_sugeridos` | 3–4, máx 40 chars, orden factual → comparativa → visual |
| `optimizacion[]` | tácticas de **ejecución** dentro de la vía recomendada; no repetir las vías |

Post-proceso **antes** de cachear (`handleAnalizar` L794–797): `completarMomentoDia` (L571) · `asegurarRecomendada` (L608) · `alinearEconomiaConAlternativa` (L630) — el resumen `economia` se pisa con la vía recomendada.

### Cómo

Orden bloqueado (`analizar.ts` `handleAnalizar` L690–821):

1. Validar `contrato_id`.  
2. Ficha (incluye `tdr_texto`, `pdf_hash`).  
3. TDR: columna `tdr_texto`, si no chunks (`limit=80`), si no ficha.  
4. Si chars &lt; **200** → **422** `sin_tdr` (L19, L716–721). **No** descuenta cupo.  
5. KV `analyze:{id}:{pdf_hash\|\|'na'}` (`cacheKey` L648–650) TTL **259200 s = 3 d** (L20, L813). HIT → 200, header `X-Analisis-Cache: HIT`, 0 Gemini, 0 cupo. En HIT se re-aplica `completarMomentoDia` (L731).  
6. `checkAnalyzeLimits` (no `flash:`).  
7. Techo TDR **60 000** chars (L18).  
8. 1× Flash JSON (`maxOutputTokens: 65536`, thinking LOW, temp 0.15, L665–670).  
9. Post-proceso + `KV.put`. No pisa HIT anteriores salvo TTL/hash nuevo.

Schema economía: `valor/costo/margen` estimados + `supuestos[]` + `lo_que_no_sabe[]`. Cifras de economía **no** están en `required` (L228: solo pistas/supuestos/lo_que_no_sabe). Techo **8 UIT = S/42 800** (L15).

**UI (prod):** `AnalisisContrato.tsx` + `AnalisisV2.tsx` (infografía ratio, N alternativas, economía por componente, contradicciones) + `TimelineFishbone.tsx` (thumbnail + fullscreen, eje con salto de plazo). Análisis **congelado** respecto de #11.

### Por qué

| Elección | Etiqueta |
|---|---|
| Caché id+hash 3 d | **HEREDADA** (id+hash) / **POR-DEFECTO** (TTL 259200) — el hash está en la clave; el 3 d no tiene eval |
| Estimación vs techo, nunca cifra seca | **HEREDADA** — criterios ENERTRONIC |
| `economia` = vía recomendada | **HEREDADA** — iter. 2.5; el chat y la página deben contar la misma historia |
| Consorcio no inferido | **HEREDADA** — ausencia ≠ permiso |
| 422 sin TDR corto | **HEREDADA** — no alucinar sobre ficha vacía |
| Cupos distintos del chat | ver G |

### Alternativas

TTL más largo si los TDR no rotan. No reabrir el schema 2º orden sin producto. La próxima palanca de costo de #10 es el MISS (1 Flash grande), no el HIT.

---

## F. Cotización asistida (#11)

### Qué hace

`POST /cotizar` `{ contrato_id, query, history? }`. Recalcula un **escenario** sobre el análisis **ya cacheado**. No RAG. No re-analiza TDR. **No cachea** la respuesta del chat (solo lee la caché #10).

### Cómo

Orden (`cotizar.ts` `handleCotizar` L541–617):

1. Validar `contrato_id` + `query` (`parseQuery` = **solo trim**, L279–281).  
2. `fetchFicha` + `cacheKey` (mismos helpers que #10).  
3. Si no hay KV → **409** `sin_analisis` (L558–562). 0 Gemini.  
4. `sanitizeHistory` (#3).  
5. `checkCotizarLimits(..., geminiCalls=2)` (`limits.ts` L162–193): IP cuenta **1** pregunta; `cotizar:{day}` cuenta **2** Flash.  
6. `generarEscenario` (L441–501):  
   - **Clasificador** = 1 Flash JSON aparte (`clasificarIntent` L391–404, `maxOutputTokens: 200`, temp 0, **sin** thinking). Devuelve `{nivel, formato, necesita_internet, razon}`. Si Gemini falla → `heuristicIntent` (L236). Post-filtro: 1 sola vía + «comparar vías» → fuerza nivel 1 texto.  
   - **Generate** = 1 Flash JSON (`COTIZAR_SCHEMA`, `maxOutputTokens: 8192`, thinking LOW, temp 0.2).  
   - Post-proceso: `normalizeEscenario` → `completarEstructuras` (arma tabla/gráfica desde `alternativas[]` / componentes si Gemini las omite) → `aplicarFormatoSugerido` → `limpiarFormatoNatural` (nivel 1 sin montos: sin supuestos, sin nota ENERTRONIC).  
7. Respuesta:  
   - `Accept: text/event-stream` → SSE de **presentación**: eventos `phase` → `text` (chunks del campo `escenario` ya completo, ~5 palabras / 22 ms) → `data` `{escenario, clasificacion}` → `done`. Tablas/gráficas **solo** en `data`, nunca a medias.  
   - Si no SSE → JSON `{ contrato_id, escenario, clasificacion }` 200.

Fail-closed (`escenario.ts` `normalizeEscenario`): si `supuestos_aplicados` no es lista no vacía, **montos = null**. Front: montos y marco «Escenario estimado» **solo** si `valor_estimado_soles != null`; nivel 1 factual = prosa limpia (`AnalisisContrato.tsx` `EscenarioCard`).

Cupos: `cotizar:ip:` RPM 8/60, `cotizar:ip:{day}` **COTIZAR_IP_RPD=20**, `cotizar:{day}` **COTIZAR_RPD=80 Δ2** (`wrangler.toml` L20–21). No toca `flash:` ni `analyze:`.

**UI (prod):** panel fijo 380 px, `key={contratoId}`, persistencia `chat_escenarios_{id}` (mensaje **completo** al terminar el stream). Desktop ≥1024 px: panel **abierto** al cargar. &lt;1024 px: cerrado + FAB. Indicador «Analizando…» (fases clasificar / contexto / redactar) → colapsa a «Analizado ✓».

### Por qué (endpoint nuevo, no el chat)

El chat RAG busca **otros** contratos. Un what-if sobre **este** TDR debe leer la caché #10. Por eso `/cotizar` no hace retrieve.

Dos Flash (clasificador + generate): **HEREDADA** de la iteración 4 (routing). El clasificador **no** es un pre-filtro de reglas: las heurísticas solo corren si Gemini tira. Streaming de presentación (no dos generaciones): **HEREDADA** de la iteración 7 — el JSON estructurado no se puede stremear a medias.

### Alternativas

Caché exacta de `(contrato_id, query)`: no está (próxima eficiencia). Pre-filtro keyword antes del clasificador: el código `heuristicIntent` ya existe como fallback; no se llama primero. Reescribir query + RAG: otra Flash, fuera de este endpoint.

---

## G. Rate-limiting y gasto

### Qué hace — tres sistemas

| Sistema | RPM (binding `CHAT_RPM`) | RPD IP (KV) | Global día (KV) | Gemini por request |
|---|---|---|---|---|
| Chat RAG | `ip:${ip}` 8/60 | `ip:${ip}:${day}` **CHAT_RPD=40** | `flash:${day}` **FLASH_RPD=200** **Δ2** | extract + generate (+ embed aparte, no en Δ2) |
| `/analizar` | `analyze:ip:${ip}` 8/60 | `analyze:ip:${ip}:${day}` **15** | `analyze:${day}` **40** | 1 generate si MISS; HIT=0 |
| `/cotizar` | `cotizar:ip:${ip}` 8/60 | `cotizar:ip:${ip}:${day}` **20** (1 pregunta) | `cotizar:${day}` **80** **Δ2** | clasificador + generate; 409=0 |

RPM: `CHAT_RPM.limit({ key })` atómico por colo (`limits.ts` L80–84, L127–131, L167–171).  
RPD: `kvBump` get+put (`limits.ts` L60–71) — **no** es transacción; dos requests concurrentes pueden pasarse 1. Periodo UTC.

Valores wrangler: `wrangler.toml` L16–21. Defaults código: `limits.ts` L16–21.

**Un solo KV namespace en el Worker:** binding `CHAT_LIMITS`, id `c63cfd497041477f91dafdde5935f37d` (`wrangler.toml` L31–33). No hay un segundo KV. Claves que conviven:

| Prefijo | Uso | TTL |
|---|---|---|
| `analyze:{id}:{hash}` | Caché JSON de `/analizar` | **259200 s** (3 d) |
| `analyze:{YYYY-MM-DD}` | Contador global ANALYZE | hasta mañana UTC |
| `analyze:ip:{ip}:{day}` | RPD IP analizar | hasta mañana UTC |
| `ip:{ip}:{day}` | RPD IP chat RAG | hasta mañana UTC |
| `flash:{day}` | Global Flash del chat (Δ2) | hasta mañana UTC |
| `cotizar:{day}` | Global Flash de `/cotizar` (Δ2) | hasta mañana UTC |
| `cotizar:ip:{ip}:{day}` | RPD IP cotizar (1 por pregunta) | hasta mañana UTC |

`/cotizar` **ya tiene** `env.CHAT_LIMITS` (lee `analyze:{id}:{hash}`). Un caché de chat cabría en el **mismo** namespace con otro prefijo (p. ej. `cotizar:q:{id}:{hash}`); no hace falta crear KV nuevo. Límite de valor KV Cloudflare: **25 MiB** por key — un JSON de escenario con tabla/gráfica (unos KB) cabe. RPM **no** vive en KV: binding `CHAT_RPM` namespace_id `8701`.

### Por qué aislados

**HEREDADA:** un usuario de Ruta no debe vaciar el chat (FLASH 200 / CHAT 40), y los what-if no deben comer el cupo caro de análisis 60k TDR (ANALYZE 40). `/cotizar` sube `cotizar:{day}` **Δ2** (clasificador+generate) y **no** `flash:` ni `analyze:`.

Backstop de facturación Gemini (~S/10/mes AI Studio): **[por confirmar con Rolando]** — no está en el código.

### 502 / JSON inválido de Gemini (contrato 66461, 18 ago 2026)

Comportamiento **real**:

| Endpoint | Si Gemini devuelve JSON roto / HTTP no OK |
|---|---|
| `/analizar` | `parseAnalisisJson` tira; catch → **HTTP 502** `{ error: msg }` (`analizar.ts` L217–221, L374–376). El 66461 fue exactamente eso. **No** hay retry. El cupo ANALYZE **sí** se gastó (límites van **antes** de Gemini). |
| `/cotizar` | Generate/parse tira; catch → **HTTP 502** JSON si aún no abrió SSE; si ya stremea → evento `{type:'error'}` con HTTP 200. Cupo cotizar (Δ2) **ya descontado**. |
| Chat JSON | catch de `handleRagJson` → **HTTP 200** con `error` + texto «No pude completar…» (`index.ts` L671–679). El front puede pintarlo como fallo blando. |
| Chat SSE | `stage: 'error'` dentro del stream HTTP 200 (`index.ts` L772–775). Front lanza y muestra error. |
| Filtros v2 | HTTP/parse malos → regex, **no** 502 (`index.ts` L401–421). |

No hay cola de reintento ni circuit breaker aparte del tope `flash:` / `analyze:` / `cotizar:`.

### Alternativas

Retry 1× en `/analizar` ante parse error (cuesta otra Flash). Idempotencia: no cobrar ANALYZE si Gemini falla — hoy **no**. Revisar FLASH_RPD si el chat Δ2 se come el día.

---

## H. Frontend y datos

### Qué hace

SPA autenticada que lee Supabase (anon + RLS) y pega al Worker.

### Cómo

**Stack:** React 19 + Vite + Tailwind + react-router + supabase-js (`seace-web/package.json`). Host: Cloudflare Pages.

**Rutas** (`App.tsx` L21–30), todas salvo login con `RequireAuth`:

| Ruta | Página |
|---|---|
| `/login` | `signInWithPassword` (`Login.tsx` L27). No hay `signUp` en el front |
| `/ruta-dia` | #9 |
| `/analisis/:id` | #10 (página) + panel #11 (380 px, `key={contratoId}`) |
| `/` | Dashboard |
| `/buscar` | Buscador |
| `/chat` | RAG + history[] |
| `/docs` | API |
| `/usuarios` | admin |

**Auth:** sesión JWT; perfil en `perfiles` (`RequireAuth.tsx`). Altas: Edge Functions `crear-usuario` / `desactivar-usuario` (service_role). Signup público: cerrado en Auth (no está en este repo).

**Tablas (las que el producto usa):**

| Tabla | Rol |
|---|---|
| `contratos` | Ficha SEACE + TDR + IT. PK = id SEACE (`supabase_schema.sql` L7–24) |
| `chunks_tdr` | texto + `embedding` 768 + `embedding_v2` 1536 + `fuente` |
| `perfiles` | `rol` admin\|normal, RLS (`seace-web/supabase/perfiles.sql`) |
| `ingesta_rechazados` | G2 dead-letter (`ingesta_rechazados.sql`) |

RLS contratos: SELECT `anon` y `authenticated` (`supabase_schema.sql` L178–185). Escritura: service_role del pipeline. `chunks_tdr` SELECT abierto (`schema_rag.sql` L42–47, `USING (true)`).

**RPC:** `buscar_tdr_v2` (vector 1536); `buscar_contratos` (FTS, `supabase_schema.sql` L72); `buscar_tdr` (768, vivo en BD, no usado por chat v2).

Worker usa `SUPABASE_ANON_KEY` (secret CF). Pipeline usa `SUPABASE_SERVICE_KEY`.

### Por qué

SPA + RLS lectura pública de datos SEACE: **HEREDADA** (comentario schema L175–176). Worker como proxy de Gemini: no exponer la key al browser.

**#11 en la UI (prod, iter. 5–7):** `AnalisisContrato.tsx` — `CHAT_PANEL_W=380`, breakpoint panel/margen `min-width: 1024px`, `chatOpen` inicial = ese matchMedia. Persistencia `localStorage['chat_escenarios_'+id]` (sin flags de streaming). Render condicional: `ChatTable` / `ChatChart`; factual sin marco de montos. El front **no** llama a Gemini.

### Alternativas

Jubilar Dashboard/Buscador si Ruta cubre el día (bugs de fecha/badge, otra decisión). No es arquitectura de retrieval.

---

## Cierre

### Tabla de decisiones

| Decisión | Estrategia actual | Cómo se decidió | Alternativa a vigilar | Cuándo reconsiderar |
|---|---|---|---|---|
| Embeddings chat | gemini-embedding-001 @1536 L2, QUERY/DOCUMENT | **MEDIDA** 27%→63% success@10 | Otro modelo / dims | Caída de success@k o costo |
| Índice v2 | HNSW coseno | **HEREDADA** PLAN D3 | ivfflat bien tuneado, otro ANN | Chunks vigentes ~10× |
| Fusión | RRF k=60 siempre | **HEREDADA** PLAN D6; k **POR-DEFECTO** | k distinto, RRF a nivel chunk | Ruido o recall |
| Umbral vector | 0.20 | **POR-DEFECTO** | Barrido 0.10–0.35 | Muchos/cero hits |
| Reranker | `bge-reranker-base` top 5 | **POR-DEFECTO**; PLAN pedía v2-m3 | v2-m3; eval post-rerank | G4 con reranker |
| LLM | gemini-3.7-flash thinking LOW | **POR-CONFIRMAR** | Flash más barato/otro | Costo o 502 JSON |
| Chunk | 800/500, **sin** overlap | **POR-DEFECTO**; ≠ PLAN 200–400. Medición ya diseñada: Tarea #4 vs 63% success@10 | #4 overlap/tamaño (eval offline) | Tras #4 |
| Embed PDF | cuerpo sin header | **MEDIDA** A/B 87164 | — | Headers API largos si molestan |
| OCR | vigentes+ventana+TI, 2 h, por página | **HEREDADA** cupo | Más cola imagen | Si 1398 no baja |
| G1 GC | flag existe, cron **no** lo usa | **POR-CONFIRMAR** | `--gc` | Chunks de culminados hinchan HNSW |
| Ruta 0–100 | sin IA; 2–7d&gt;hoy | **HEREDADA** criterios | Señales #10 | Si el ranking miente vs margen |
| #10 caché | `analyze:id:hash` 3 d | **HEREDADA** (clave) / **POR-DEFECTO** (TTL) | TTL | PDF que cambia seguido |
| #11 | `/cotizar` clasificador+generate Δ2, SSE presentación, sin caché de chat | **HEREDADA** iter. 4–7 | Pre-filtro reglas; caché exacta query | Eficiencia de tokens |
| Cupos | 3 **prefijos** en el mismo KV `CHAT_LIMITS` | **HEREDADA** | KV aparte para chat | Si un cupo queda muerto y otro explota |
| 502 Gemini | analizar/cotizar 502; chat 200+texto | **POR-DEFECTO** (catch) | Retry 1× | Si 66461-like se repite |

### Discrepancias código vs PLAN

1. Reranker: PLAN `bge-reranker-v2-m3` · código `@cf/baai/bge-reranker-base`.  
2. Chunks: PLAN 200–400 **con solape** · código 800/500 **sin** overlap.  
3. G1 GC: PLAN borra chunks al cerrar · cron **sin** `--gc`.  
4. Eval 63%: PLAN habla del Worker completo · G4 **sin** reranker.  
5. Caché semántico / exacta de queries (PLAN Fase 7 opcional): **no** está. Hay caché de `/analizar` por contrato (`analyze:{id}:{hash}`), **cero** `KV.put` de respuestas `/cotizar`. La query de #11 solo se `trim()`.  
6. Drop `embedding(768)` + ivfflat (PLAN Fase 7): **no** hecho; v1 sigue en BD, chat no lo usa.  
7. Header contextual en **todos** los chunks (PLAN Fase 4): PDF embebe **cuerpo**; API sigue con header largo.

### Commits de referencia (corte 20 ago 2026)

| Repo | HEAD | Qué fija |
|---|---|---|
| monitor | `e5f58d6` | Pipeline + docs (este corte) |
| worker | `437c265` | `/cotizar` SSE + prompt formato natural; clasificador Δ2 desde `28eaf4b` |
| web | `9426dc1` | Panel default desktop + streaming + Analizando + andamiaje vacío |

Iteraciones 1–7 (detalle en `ESTADO_CIERRE_2026-08.md`): worker `75e84af` → `437c265`; web `4a6742f` → `9426dc1`.

Snapshot: 20 ago 2026.
