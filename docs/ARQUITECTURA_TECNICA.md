# Arquitectura técnica — SEACE Monitor

Documento de **cómo funciona hoy** cada pieza, con evidencia `archivo:línea`.
No sustituye al PLAN: si código y PLAN divergen, se anota aquí.

- Foto de prod: [ESTADO_CIERRE_2026-08-29.md](./ESTADO_CIERRE_2026-08-29.md) (histórico 1–9: [ESTADO_CIERRE_2026-08-20.md](./ESTADO_CIERRE_2026-08-20.md))
- Historia de sprints: [CHANGELOG_ITERACIONES.md](./CHANGELOG_ITERACIONES.md)
- Punto de entrada: [TRASPASO_MAESTRO_SEACE.md](./TRASPASO_MAESTRO_SEACE.md)

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
I. Capa semántica (SQL + Dashboard)  
J. Funnel de conversión (#10/#11 → PG → Dashboard)  
Cierre: tabla de decisiones · discrepancias vs PLAN · commits

---

## 0. Snapshot y repos

| Pieza | Path | HEAD documentado |
|---|---|---|
| Pipeline / SQL / evals | `seace-monitor` | `a060d2a` + este paquete de docs |
| Worker | `seace-ai-proxy` | `fdcc7fd` (self-routing + funnel + cleanup) · CF **`fcefa7a0-e623-432b-8479-71b576927cda`** |
| Front | `seace-web` | `6f1a2f9` (Dashboard conversión 30d) · Pages `https://seace.rdiaz-lab.xyz` |

Worker vivo: `https://seace-ai-proxy.rdiazg14.workers.dev`. Front: `AI_PROXY` = esa URL (`seace-web/src/lib/supabase.ts` L7).

Fecha de este corte: **29 ago 2026** (Perú). Asesor #9/#10/#11 **iteraciones 1–11** en prod.

Etiquetas de «por qué»:

- **MEDIDA** — hay número en eval/A-B/código o en un A/B corrido (p. ej. `eval_v2.json`).
- **HEREDADA** — está en PLAN/criterios o se implementó a propósito, **sin** re-medir en G4.
- **POR-DEFECTO** — valor típico o el que quedó en el archivo; no hay A-B.
- **POR-CONFIRMAR** — no consta en código ni eval; no se inventa.

---

## 0.5 Escala (ancla)

Números de producto en este corte:

| Universo | Magnitud |
|---|---|
| Contratos en BD | **76 509** (pipeline 20 ago, commit `dcf0a29`: nuevos=95, OCR=completo) |
| Vigentes | ~2 083 (ancla 18 ago; no se re-contó en este cierre) |
| Chunks con `embedding_v2` | ~9 169 (ancla; backfill PDF `e5f58d6`) |
| PDF | 679 nativo / 102 mixto / 1 398 imagen / 150 sin_pdf (ancla 18 ago) |

Las decisiones de esta arquitectura se consideran **adecuadas bajo esta escala**. Si el corpus o los chunks vigentes crecen **~10×**, hay que reabrir al menos: HNSW vs otro índice, threshold 0.20, tamaño de chunk, RRF k, tope OCR 2 h, FLASH_RPD=200, y si el reranker `-base` aguanta.

---

## A. Retrieval / RAG

### Qué hace

El chat `POST /` (no `/analizar` ni `/cotizar`) arma contexto TDR y genera con Gemini Flash. Backend de prod: `RAG_BACKEND=v2` (`seace-ai-proxy/wrangler.toml` L13).

Pipeline v2, en orden (`retrieveContextV2`, `index.ts` L546–589):

1. `extraerFiltrosV2` — 1× Flash JSON (estado / término / entidad).  
2. `embedQueryGemini` — Gemini embed `RETRIEVAL_QUERY` 1536 + L2 del **término extraído** (`filtros.termino || query`, L549), no del hilo.  
3. En paralelo: RPC `buscar_tdr_v2` (20 chunks, umbral) + RPC `buscar_contratos` FTS (20).  
4. RRF a **nivel contrato** (no chunk), top 20.  
5. Hasta 2 chunks vectoriales por contrato (o el hit FTS si no hay vector).  
6. `rerankTop` Workers AI → 5 ítems.  
7. `geminiGenerate` — 1× Flash (SSE o JSON) con fragmentos + query (+ `history[]` opcional).

`reserveFlash(env, 2)` cuenta extract+generate (`index.ts` L896–897). El embed **no** entra en ese delta 2.

### Cómo (valores reales)

| Paso | Valor | Evidencia |
|---|---|---|
| Embed modelo | `gemini-embedding-001` | `index.ts` L97, L434–436 |
| Dims | 1536, recorte si viene de más | `GEMINI_DIM` L100 |
| Query task | `RETRIEVAL_QUERY` | `embedQueryGemini` L434 |
| Documento (pipeline) | `RETRIEVAL_DOCUMENT` | `generar_embeddings.py` L244, L468 |
| Norma | L2 en query y en documentos | `index.ts` L154–160; `generar_embeddings.py` `l2_normalize` |
| Índice vector | HNSW coseno sobre `embedding_v2` | `docs/schema_rag_v2.sql` L53–55; RPC `buscar_tdr_v2.sql` L4–5, L31–38 |
| Índice v1 (aún en BD) | ivfflat `embedding(768)` lists=100 | `schema_rag.sql` L33–36. **No** lo usa el chat con `RAG_BACKEND=v2` |
| Default código si falta env | `ragBackend` cae a **v1** | `index.ts` L131–132. Prod fija `RAG_BACKEND=v2` en wrangler L13 |
| Distancia RPC | `<=>` pgvector; similarity = `1 - dist` | `buscar_tdr_v2.sql` L31 |
| Umbral v2 | `0.20` (`SIMILARITY_THRESHOLD_V2`) | `wrangler.toml` L12; default código L144–146; SQL default L13 |
| Umbral v1 (apagado) | `0.70` toml / default código **0.80** | `wrangler.toml` L11; `index.ts` L139–141 |
| RRF k | **60** | `RRF_K` `index.ts` L99, L480 |
| Reranker | `@cf/baai/bge-reranker-base`, top_k=5, texto ≤1500 | L96, L495 |
| Fallback rerank | si CF falla → primeros 5 del RRF | `rerankTop` |
| LLM | `GEMINI_FLASH_MODEL=gemini-3.7-flash`, thinking `LOW`, temp 0.2, max 2048 | `wrangler.toml` L14; `index.ts` L657 |
| Filtros | Flash JSON; si falla HTTP/parse → regex `extraerTermino` / `extraerEntidad`, estado default Vigente | L382–431 |
| History #3 | sanitizado máx 8×500; **después** del retrieve; embed/filtros ven la query actual (término), no el hilo | `history.ts` L9–13; `userPromptFrom` L627–637 |

`POST /embed` (BGE 768) responde **410** (`index.ts` L162–167).

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
| RRF siempre (no fallback «si &lt;3 chunks») | **HEREDADA** | PLAN D6; código L550–581 siempre fusiona |
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
| 9 | Funnel | `reconciliar_funnel.py` | `continue-on-error`; GET `/funnel-pendientes`; upsert ISO del KV |
| 10 | G3 | `alerta_g3.py` por paso si falla + alerta final `always()` | incluye `--paso funnel` |

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

`/ruta-dia` rankea oportunidades **sin IA**, 0–100, desde columnas de `contratos`. Es la pantalla de **acción diaria**. Definición de producto (Rolando): **solo postulables** en el ranking default. Desde `cffcc2b` eso está en código: `esPostulable()` es la fuente única.

### Cómo

Fórmula (`rutaDia.ts` L4–12, L247–272):

`score = rubro(50) + vigencia(25) + urgencia(15) + señales(10)` (tope 100).

| Bloque | Puntos | Código |
|---|---|---|
| Rubro | Núcleo 50 / Adyacente 38 / Oportunista 24 / Marginal 12 | `PTS_RUBRO` L102–107 |
| Vigencia | Vigente 25 / En Evaluación 12 | L255 |
| Urgencia (solo Vigente, ventana no vencida) | hoy **10** · mañana **12** · **2–7 d = 15** · 8–30 d 8 · &gt;30 d 5 · sin fecha 3 · vencido 0 | `ptsUrgencia` L225–235 |
| Señales | ALTA+IA real 6 / MEDIA 3 / BAJA 1 · objeto Servicio +2 (máx 10) | L237–244 |

Mapeo `categoria_it` → nivel: L79–93. Overlay texto: telemetría/SCADA/OT/IoT → Núcleo; integración/automatización/digital twin → Adyacente; **nunca baja** (L215–216). Firma digital + ALTA **no** sube a Núcleo (`cat !== 'Firma digital'`, L218–219).

**Universo SQL** (`RutaDia.tsx` `fetchUniverso` ~L28–46): `estado IN ('Vigente','En Evaluación')` **y** (`categoria_it` OR `relevancia_ia` no null). Culminado no entra. El recorte a postulables es **en el cliente**, no en el SELECT.

**`esPostulable`** (`rutaDia.ts` L276–285) — **única definición**:

```
estado === 'Vigente' && (fecha_fin es null || fecha_fin Lima >= hoy Lima)
```

`puntuar` L250 usa esa función. En Evaluación **nunca** es postulable.

**`rankingActivo`** (L287–292): deja Vigente (incl. vencidos) + En Evaluación, ordenados por score. **No** recorta. El recorte lo hace `aplicarFiltros` con `estado` default `'postulable'` (`RutaDia.tsx` L55).

### Postulabilidad (código 20 ago noche, `cffcc2b`) — una función

| Definición | Dónde | Qué incluye |
|---|---|---|
| Flag / filtros / KPIs / brief | `esPostulable()` | solo Vigente con ventana abierta o sin fecha |
| SQL universo | `fetchUniverso` | Vigente **y** En Evaluación (IT/IA) — materia prima |
| Ranking puntuado | `rankingActivo` | mismo universo; el **chip** recorta |

Por bloque de UI (`RutaDia.tsx`):

| Bloque | Fuente | ¿Cuela no-postulables? |
|---|---|---|
| KPIs (nuevos hoy, cierran, núcleo) | `scored.filter(o => o.postulable)` L93 | **No** |
| Brief Top 15 | `aplicarFiltros(..., estado: 'postulable')` L87–89 | **No** |
| Ranking default | `estado === 'postulable'` L55, L82–84 | **No** |
| Chip «En evaluación / cerrados» | `estado === 'cerrados'` L233–235 | **Sí, a propósito**: En Evaluación + vigentes con ventana vencida |

`aplicarFiltros` L311–312: `cerrados` = `!esPostulable`. Copy L186: «Por defecto solo postulables».

### vs Dashboard (iter. 9)

Ambos usan la **misma** regla de postulable (SQL `es_postulable` ≡ `esPostulable()`). Dashboard ya no calcula «cierran hoy/semana» metiendo vencidos. Detalle: §I.

### Por qué

| Elección | Etiqueta | Texto en código |
|---|---|---|
| Sin IA | **HEREDADA** | L2: «100% desde BD, sin IA». Fuente: `docs/CRITERIOS_DECISION_ENERTRONIC.md` |
| 2–7 d &gt; hoy | **HEREDADA** | L11: «cierra hoy es bandera, no dominancia» |
| Firma digital no sube | **HEREDADA** | L23: token cripto ≠ tokens de IA |
| Modalidad/pago/margen = 0 aquí | **HEREDADA** | L14: eso vive en #10 |
| Default solo postulables | **HEREDADA** (criterio Rolando 20 ago) | `cffcc2b`; chip para ver cerrados |

### Alternativas

Exigir fecha (hoy sin fecha cuenta postulable). Meter señales de #10 al score. Home = esta página (sigue siendo `/` Dashboard).

---

## E. Análisis (#10)

### Qué hace

`POST /analizar` `{ contrato_id }` → JSON estructurado para ENERTRONIC. UI: `/analisis/:id`. Una llamada Flash **solo en MISS** de caché.

El schema **required** sigue siendo las 5 secciones + `resumen` + `optimizacion` (`analizar.ts` L423). Encima, el system prompt exige razonamiento de 2º orden (campos opcionales en el schema JSON, obligatorios en instrucciones):

| Campo | Contenido |
|---|---|
| `estructura_contractual.entregables[]` | plazos y `riesgo_penalidad` |
| `componentes_servicio[]` | cursos/lotes/ítems del TDR |
| `requisitos_proveedor` | habilitaciones, experiencia, certs; `admite_consorcio` true solo si el TDR lo dice; **`null` si no consta** |
| `riesgos_contractuales` | fórmula F, IP, plataforma, `clausulas_criticas[]` |
| `timeline.hitos[]` | Secuencia variable: `orden`, `nombre`, `tipo`, `momento_texto`, `momento_dia` (**siempre estimado**, prompt + `completarMomentoDia` L571), `tiene_pago`, `es_critico` |
| `viabilidad.ratio_alcance` | `valor_mercado_min/max`, `techo_contrato`, `ratio_texto`, `lectura` |
| `viabilidad.cotizacion_por_componente[]` | `componente`, `mercado_min/max` |
| `viabilidad.contradicciones_tdr[]` | inconsistencias internas; array vacío válido |
| `alternativas[]` | N variable (1 si la directa es viable; 2–3 si no). Exactamente una `recomendada=true`. Cada una con `economia.{valor,costo,margen}` |
| `chips_sugeridos` | 3–4, máx 40 chars, orden factual → comparativa → visual |
| `optimizacion[]` | tácticas de **ejecución** dentro de la vía recomendada; no repetir las vías |

Post-proceso **antes** de cachear (`handleAnalizar` L794–797): `completarMomentoDia` (L571) · `asegurarRecomendada` (L608) · `alinearEconomiaConAlternativa` (L630) — el resumen `economia` se pisa con la vía recomendada.

### Cómo

Orden bloqueado (`analizar.ts` `handleAnalizar` L690–821):

1. Validar `contrato_id`.  
2. Ficha (incluye `tdr_texto`, `pdf_hash`).  
3. TDR: columna `tdr_texto`, si no chunks (`limit=80`), si no ficha.  
4. Si chars &lt; **200** → **422** `sin_tdr` (L19, L716–721). **No** descuenta cupo.  
5. KV `analyze:{id}:{pdf_hash\|\|'na'}` (`cacheKey` L649) TTL **259200 s = 3 d** (L21, L816). HIT → 200, header `X-Analisis-Cache: HIT`, 0 Gemini, 0 cupo. En HIT se re-aplica `completarMomentoDia` y **sí** `marcarFunnel(..., 'analizado')` (L734/L744).  
6. `checkAnalyzeLimits` (no `flash:`).  
7. Techo TDR **60 000** chars (L19).  
8. 1× Flash JSON (`maxOutputTokens: 65536`, thinking LOW, temp 0.15).  
9. Post-proceso + `KV.put` + `marcarFunnel` analizado (L818). No pisa HIT anteriores salvo TTL/hash nuevo. 502 **no** marca funnel.

Schema economía: `valor/costo/margen` estimados + `supuestos[]` + `lo_que_no_sabe[]`. Cifras de economía **no** están en `required` (L228: solo pistas/supuestos/lo_que_no_sabe). Techo **8 UIT = S/42 800** (L15).

**UI:** `AnalisisContrato.tsx` + `AnalisisV2.tsx` (infografía ratio, N alternativas dinámicas, economía por componente, contradicciones) + `TimelineFishbone.tsx` (thumbnail + fullscreen, eje con salto de `momento_dia`). Panel #11 380 px, `key={contratoId}`, abierto por defecto en desktop ≥1024 px. Análisis **congelado** respecto de #11.

502 (JSON Gemini inválido, p. ej. contrato **66461**): HTTP 502 con cuerpo estructurado (`analizar.ts` L827–835):

```
{ error: 'analisis_fallido', mensaje, reintentar: true, detalle_tecnico }
```

El front (`AnalisisContrato.tsx`) muestra banner amable + **Reintentar** (`fetchAnalisis`); **no** pinta `detalle_tecnico` ni el JSON crudo. El cupo ANALYZE **ya se gastó** (límites van antes de Gemini). No hay retry en el Worker.

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

`POST /cotizar` `{ contrato_id, query, history? }`. Recalcula un **escenario** sobre el análisis **ya cacheado**. No RAG. No re-analiza TDR.

Desde iter. 10: **self-routing**. El generate único elige `tipo_respuesta` (`texto` | `tabla` | `grafica` | `tabla_grafica`). El clasificador Flash y `clasificarPorReglas` **ya no existen**. Caché exacta de respuestas factuales (`esCacheable`) sigue.

### Cómo

Orden (`cotizar.ts` `handleCotizar` L602–):

1. Validar `contrato_id` + `query`.  
2. `fetchFicha` + clave #10 `analyze:{id}:{pdf_hash}`.  
3. Si no hay KV → **409** `sin_analisis` (L619–623). 0 Gemini. **No** marca funnel.  
4. `sanitizeHistory` (#3).  
5. **Caché chat** (`chatCacheKey`): `chat:{contratoId}:{pdf_hash}:{sha256(normalizarPregunta(query))}`. `normalizarPregunta` L200–201 = trim + lowercase + colapsar espacios; **no** quita tildes. TTL **259200 s = 3 d** (`CHAT_CACHE_TTL` L195).  
   - HIT → solo RPM (`checkCotizarRpm`); **0** Flash; **no** descuenta RPD/global; `kvIncr chat_cache:hit:{day}`; **`marcarFunnel(..., 'cotizado')`** (L652). Headers `X-Cotizar-Cache: HIT`, `X-Cotizar-Intent` = el que se guardó (`reglas` solo si la entrada es anterior al cleanup). SSE o JSON. **Ignora `history[]`.**  
   - MISS → `kvIncr chat_cache:miss:{day}`.  
6. `checkCotizarLimits(..., geminiCalls=1)` L687. IP cuenta 1 pregunta; `cotizar:{day}` cuenta **Δ1**.  
7. `generarEscenario` (L466–510): **un** Flash JSON (`COTIZAR_SCHEMA`, 8192, thinking LOW, temp 0.2). No hay `intentPrevio` ni `clasificarIntentFlash`.  
   - Post-proceso: `normalizeEscenario` → `completarEstructuras` **gateado por el `tipo_respuesta` crudo del modelo** (L501–503; no se recortan visuales que el modelo pidió) → `tipoDesdeDatos` → `limpiarFormatoNatural` (sin “nivel”).  
   - `clasificacionDesdeTipo` (L456) arma `{nivel, formato}` para el front a partir del tipo final. `intentSource` MISS = `'flash'`.  
8. `finish` (L699): **`marcarFunnel` cotizado** (independiente de cacheable) → `kvIncr cotizar_tipo:{tipo}:{day}` → `saveChatCache` solo si `esCacheable` (L204–209): **false** si `supuestos_aplicados.length > 0` o hay monto (`valor`/`costo`/`margen` no null).  
9. Respuesta: SSE de **presentación** (texto ya completo, ~5 palabras / 22 ms; tablas/gráficas solo en evento `data`) o JSON. Headers `X-Cotizar-Cache: MISS`, `X-Cotizar-Intent: flash`.

`parseIntent` / `IntentCotizar` / `IntentSource` **siguen vivos** para leer HIT de caché (entradas viejas pueden tener `intent: reglas`).

Fail-closed (`escenario.ts` `normalizeEscenario` L222): si `supuestos_aplicados` no es lista no vacía, **montos = null**.

Cupos: `cotizar:ip:` RPM 8/60, `cotizar:ip:{day}` **20**, `cotizar:{day}` **80 Δ1**. No toca `flash:` ni `analyze:`.

**UI:** panel fijo 380 px, `key={contratoId}`, persistencia `chat_escenarios_{id}`. Desktop ≥1024 px abierto. Indicador Analizando… → Analizado ✓.

### Por qué (endpoint nuevo, no el chat)

El chat RAG busca **otros** contratos. Un what-if sobre **este** TDR lee la caché #10.

Self-routing (iter. 10): el clasificador Flash era Δ2 y además recortaba visuales. Un generate elige formato; `completarEstructuras` respeta lo que pidió el modelo. Caché exacta: repetir «¿dónde se presta?» no paga generate. No cachear escenarios con supuestos: el número estimado no debe fosilizarse. Funnel: toda cotización **exitosa** (HIT o MISS) cuenta, no solo las cacheables.

Streaming de presentación (iter. 7): el JSON estructurado no se stremea a medias.

### Alternativas

Caché semántica (parafraseo): no está. Reescribir query + RAG: otra Flash, fuera de este endpoint. Incluir history en la clave de caché: hoy HIT ignora el hilo. Reintroducir clasificador: **no**, salvo pedido explícito.

---

## G. Rate-limiting y gasto

### Qué hace — tres sistemas

| Sistema | RPM (binding `CHAT_RPM`) | RPD IP (KV) | Global día (KV) | Gemini por request |
|---|---|---|---|---|
| Chat RAG | `ip:${ip}` 8/60 | `ip:${ip}:${day}` **CHAT_RPD=40** | `flash:${day}` **FLASH_RPD=200** **Δ2** | extract + generate (+ embed aparte, no en Δ2) |
| `/analizar` | `analyze:ip:${ip}` 8/60 | `analyze:ip:${ip}:${day}` **15** | `analyze:${day}` **40** | 1 generate si MISS; HIT=0 |
| `/cotizar` | `cotizar:ip:${ip}` 8/60 | `cotizar:ip:${ip}:${day}` **20** (1 pregunta) | `cotizar:${day}` **80** **Δ1** | HIT caché: 0 Gemini (solo RPM). MISS: **1** generate. 409=0 |

RPM: `CHAT_RPM.limit({ key })` atómico por colo (`limits.ts` L80–84, L127–131, L167–171).  
RPD: `kvBump` get+put (`limits.ts` L60–71) — **no** es transacción; dos requests concurrentes pueden pasarse 1. Periodo UTC.

Valores wrangler: `wrangler.toml` L16–21. Defaults código: `limits.ts` L16–21.

**Un solo KV namespace en el Worker:** binding `CHAT_LIMITS`, id `c63cfd497041477f91dafdde5935f37d` (`wrangler.toml` L31–33). No hay un segundo KV. Claves que conviven:

| Prefijo | Uso | TTL |
|---|---|---|
| `analyze:{id}:{hash}` | Caché JSON de `/analizar` | **259200 s** (3 d) |
| `chat:{id}:{hash}:{sha256}` | Caché JSON de `/cotizar` si `esCacheable` | **259200 s** (3 d) |
| `funnel:analizado:{id}` | Marca permanente #10 | **ninguno** |
| `funnel:cotizado:{id}` | Marca permanente #11 | **ninguno** |
| `analyze:{YYYY-MM-DD}` | Contador global ANALYZE | hasta mañana UTC |
| `analyze:ip:{ip}:{day}` | RPD IP analizar | hasta mañana UTC |
| `ip:{ip}:{day}` | RPD IP chat RAG | hasta mañana UTC |
| `flash:{day}` | Global Flash del chat (Δ2) | hasta mañana UTC |
| `cotizar:{day}` | Global Flash de `/cotizar` (**Δ1**) | hasta mañana UTC |
| `cotizar:ip:{ip}:{day}` | RPD IP cotizar (1 por pregunta) | hasta mañana UTC |
| `chat_cache:hit:{day}` / `miss:{day}` | Instrumentación HIT/MISS chat | hasta mañana UTC |
| `cotizar_tipo:{tipo}:{day}` | Instrumentación `tipo_respuesta` | hasta mañana UTC |

`/cotizar` lee `analyze:{id}:{hash}` **y** escribe `chat:…` en el mismo namespace. Límite de valor KV Cloudflare: **25 MiB** por key. RPM **no** vive en KV: binding `CHAT_RPM` namespace_id `8701`.

CORS expone `X-Analisis-Cache`, `X-Cotizar-Cache`, `X-Cotizar-Intent` (`index.ts` L108). Métodos CORS: GET, POST, OPTIONS (L106). `GET /funnel-pendientes` usa `Authorization` / `X-Funnel-Token`.

### Por qué aislados

**HEREDADA:** un usuario de Ruta no debe vaciar el chat (FLASH 200 / CHAT 40), y los what-if no deben comer el cupo caro de análisis 60k TDR (ANALYZE 40). `/cotizar` sube `cotizar:{day}` **Δ1** y **no** `flash:` ni `analyze:`. HIT de caché chat no toca esos contadores (sí RPM). Funnel **no** descuenta cupo.

Backstop de facturación Gemini (~S/10/mes AI Studio): **[por confirmar con Rolando]** — no está en el código.

### 502 / JSON inválido de Gemini (contrato 66461, 18 ago 2026)

Comportamiento **real**:

| Endpoint | Si Gemini devuelve JSON roto / HTTP no OK |
|---|---|
| `/analizar` | `parseAnalisisJson` tira; catch → **HTTP 502** `{ error: 'analisis_fallido', mensaje, reintentar: true, detalle_tecnico }` (`analizar.ts` L827–835). El 66461 fue el JSON crudo `{ error: msg }`; eso **ya no** se sirve. **No** hay retry. El cupo ANALYZE **sí** se gastó. Funnel **no** se marca. Front: banner + Reintentar (`AnalisisContrato.tsx`). |
| `/cotizar` | Generate/parse tira; catch → **HTTP 502** JSON si aún no abrió SSE; si ya stremea → evento `{type:'error'}` con HTTP 200. Cupo cotizar (Δ1) **ya descontado**. Funnel **no** se marca en 502. |
| Chat JSON | catch de `handleRagJson` → **HTTP 200** con `error` + texto «No pude completar…» (`index.ts` L720–725). El front puede pintarlo como fallo blando. |
| Chat SSE | `stage: 'error'` dentro del stream HTTP 200 (`index.ts` L824–827). Front lanza y muestra error. |
| Filtros v2 | HTTP/parse malos → regex, **no** 502 (`index.ts` L414–430). |

No hay cola de reintento ni circuit breaker aparte del tope `flash:` / `analyze:` / `cotizar:`.

### Alternativas

Retry 1× en `/analizar` ante parse error (cuesta otra Flash). Idempotencia: no cobrar ANALYZE si Gemini falla — hoy **no**. Revisar FLASH_RPD si el chat Δ2 se come el día. Aligerar `v_kpis_dashboard` si el timeout 57014 se vuelve frecuente.

---

## H. Frontend y datos

### Qué hace

SPA autenticada que lee Supabase (anon + RLS) y pega al Worker.

### Cómo

**Stack:** React 19 + Vite + Tailwind + react-router + supabase-js (`seace-web/package.json`). Host: **GitHub Pages** (no Cloudflare Pages), dominio `seace.rdiaz-lab.xyz`.

**Rutas** (`App.tsx` L21–30), todas salvo login con `RequireAuth`. **Home `/` = Dashboard** (L23). Navbar lista Ruta del día primero (`Navbar.tsx` L7) pero el logo y `Navigate` fallback van a `/`. Gotcha vigente.

| Ruta | Página |
|---|---|
| `/login` | `signInWithPassword` (`Login.tsx` L27). No hay `signUp` en el front |
| `/ruta-dia` | #9 |
| `/analisis/:id` | #10 (página completa) + panel #11 (380 px, `key={contratoId}`, streaming) |
| `/` | Dashboard (capa semántica, §I) |
| `/buscar` | Buscador (`CatItIaPill`, un badge) |
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
| `v_contratos_estado` / `v_kpis_*` | Capa semántica (iter. 9, §I). No son tablas base. |

RLS contratos: SELECT `anon` y `authenticated` (`supabase_schema.sql` L178–185). Escritura: service_role del pipeline. `chunks_tdr` SELECT abierto (`schema_rag.sql` L42–47, `USING (true)`).

**RPC:** `buscar_tdr_v2` (vector 1536); `buscar_contratos` (FTS, `supabase_schema.sql` L72); `buscar_tdr` (768, vivo en BD, no usado por chat v2).

Worker usa `SUPABASE_ANON_KEY` (secret CF). Pipeline usa `SUPABASE_SERVICE_KEY`.

### Por qué

SPA + RLS lectura pública de datos SEACE: **HEREDADA** (comentario schema L175–176). Worker como proxy de Gemini: no exponer la key al browser.

**#10+#11 en la UI (`/analisis/:id`):** infografía de ratio, N alternativas dinámicas, economía por componente, contradicciones TDR, timeline fishbone (`TimelineFishbone.tsx`). Chat panel lateral 380 px atado al contrato (`key={contratoId}`), abierto default desktop ≥1024 px, SSE de presentación, persistencia `localStorage['chat_escenarios_'+id]`. 502 de `/analizar`: banner ámbar + Reintentar (no JSON crudo). El front **no** llama a Gemini.

**Buscador `/buscar`:** un solo badge `CatItIaPill` (`Pills.tsx` L42–53): si hay `categoria_it` se pinta IT; si no, IA. Nunca ambos (`6ba2eeb`). Misma pill en `ContratoCard`, `OportunidadCard`, análisis.

### Alternativas

Jubilar Dashboard/Buscador si Ruta cubre el día. Home = Ruta (sigue pendiente). No es arquitectura de retrieval.

---

## I. Capa semántica (SQL + Dashboard)

### Qué hace

Una sola definición de «postulable / cierra hoy / rubro ENERTRONIC» en Postgres, para que Dashboard y Ruta no diverjan. Iteración 9. SQL: `seace-monitor/capa_semantica.sql` (repo raíz, no `docs/`). Front: `seace-web/src/lib/capaSemantica.ts`.

**Aplicado** en Supabase `wusywwhcyqngnpvpzxyr` (vistas existen; GRANT a `anon`+`authenticated`; `NOTIFY pgrst`).

### Cómo

Día = `timezone('America/Lima', now())::date` (`seace_hoy_lima`). Misma regla que `limaDateISO()` / `esPostulable()`.

| Objeto | Rol |
|---|---|
| `fn_rubro_energetic(...)` | Gemelo de `clasificarNivel()`: línea `categoria_it` + overlay texto (telemetría→núcleo, integración→adyacente, nunca degrada) + ALTA con IA real salvo Firma digital |
| `v_contratos_estado` | Universo IT/IA + flags `es_postulable`, `es_vigente_ventana_vencida`, `es_en_evaluacion`, `cierra_hoy` / `cierra_manana` / `cierra_semana` (días **2–7**) / `cierra_7d` / `es_nuevo_hoy`. `rubro` aquí = **solo** mapeo de línea (barato, sin regex) |
| `v_kpis_dashboard` | Agregados del tablero sobre postulables + conteos de vencidos/en evaluación/culminados + `por_linea` / `por_rubro` (rubro = `fn_rubro_energetic`, solo sobre postulables) |
| `v_kpis_negocio` | Núcleo/adyacente/oportunista/marginal + IA/cloud/dev/tel sobre postulables |

Dashboard (`Dashboard.tsx`): `cargarCapaSemantica()` prefiere SQL; si `v_kpis_dashboard` falla o timeout → **fallback TS** (`fuente: 'sql' \| 'ts'`) con `esPostulable` + `clasificarNivel`. Lista default `vista=postulable`. Filtro urgencia: `cierra_hoy \|\| cierra_manana` (hoy); `cierra_7d` (semana); mes = `days >= 0 && days <= 30`. Chip «En evaluación / vencidos» = `cerrados`.

**Ruta del día no lee estas vistas** (sigue puntuando en el browser). Las reglas coinciden por diseño.

### Matiz de prod (20 ago noche)

`v_contratos_estado` y `v_kpis_negocio` responden. Un `SELECT *` de `v_kpis_dashboard` puede **timeout** (Postgres 57014). En ese caso el Dashboard usa TS y **no** se rompe. Backlog: aligerar esa vista.

Las columnas `analizado`/`cotizado` **sí existen** desde iter. 11 (`docs/migracion_funnel_conversion.sql`). No hay monto referencial. Detalle: §J.

### Por qué

**HEREDADA** (iter. 9): el leak de Dashboard (vencidos en «esta semana» / filtro hoy) venía de calcular fechas en TS distinto a Ruta. SQL + `esPostulable` unifican.

### Alternativas

Materializar KPIs (tabla+cron) si el timeout molesta. Que Ruta también lea `v_contratos_estado`. Chat que responda KPIs: no está.

---

## J. Funnel de conversión (#10/#11 → PG → Dashboard)

### Qué hace

Tres eslabones: (1) el Worker marca en KV la **primera** vez que un contrato se analiza o se cotiza con éxito; (2) el cron copia esas marcas a `contratos`; (3) el Dashboard lee tasas 30d con **dos denominadores**.

### Cómo

**KV** (`funnel.ts`): claves `funnel:analizado:{id}` / `funnel:cotizado:{id}`. Valor = ISO de la primera marca. **Sin TTL**. Idempotente (GET antes de PUT; no pisa fecha). Fallo de KV no se propaga. Prefijo distinto de `analyze:` / `chat:` / cupos.

Quién marca:

| Evento | Marca | No marca |
|---|---|---|
| `/analizar` HIT o MISS 200 | `analizado` | 422 `sin_tdr`, 502, cupo |
| `/cotizar` HIT o MISS 200 | `cotizado` (aunque no sea `esCacheable`) | 409 `sin_analisis`, 502 |

**GET `/funnel-pendientes`:** Bearer `FUNNEL_TOKEN` (secret CF). Respuesta `{ analizados: [{id, fecha}], cotizados: [...] }`. 401 si token mismatch; 503 si no hay KV. No es CORS del SPA (el front no llama esto).

**Reconciliación** (`reconciliar_funnel.py`, cron tras embed, `continue-on-error`): fusiona por id, upsert lotes de 100, **copia el ISO del KV** (no `now()`). `--dry-run` / `--limit`. Secret GitHub `FUNNEL_TOKEN` (mismo valor que CF).

**SQL:** `docs/migracion_funnel_conversion.sql` (columnas, ya en prod). `docs/vista_kpis_conversion.sql` (vistas, ya en prod + GRANT anon). Universo = INNER JOIN `v_contratos_estado` (IT) + `fecha_publica >= now() - 30 days`. `es_postulable` **no** se reimplementa. Rubro = `v.rubro` (línea, no `fn_rubro_energetic`).

Dos bloques de tasas (0..1, `numeric(4)`; NULL si denominador 0):

| Bloque | Denominador | Pregunta |
|---|---|---|
| Cobertura | `rankeados_30d` (radar IT publicado) | ¿Cuánto del radar se tocó? |
| Ejecución | `postulables_30d` (Ruta) | ¿Cuánto de lo accionable se trabajó? |

**UI** (`cargarKpisConversion`, `Dashboard.tsx` `ConversionBlock`): falla suave → null → «Sin datos de conversión todavía» (no fallback TS). `fmtTasa(null)` → "—", nunca 0%.

### Por qué

**HEREDADA** (iter. 11): el sesgo de marcar solo `esCacheable` dejaba fuera los what-if (justamente los que cotizan). HIT también cuenta: reabrir un análisis viejo es trabajo real. Las vistas viven **fuera** de `capa_semantica.sql` para no `DROP CASCADE` las de iter. 9.

### Alternativas

TTL en funnel: no (perdería historia). Backfill pre-columna: no (FALSE ≠ “nunca en la vida”). Materializar tasas: no hace falta a esta escala.

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
| Ruta 0–100 | sin IA; 2–7d&gt;hoy. Default **solo postulables** (`esPostulable`). Chip para En evaluación / cerrados | **HEREDADA** `cffcc2b` | Exigir fecha; home = Ruta | Si el chip no se usa |
| #10 caché | `analyze:id:hash` 3 d | **HEREDADA** (clave) / **POR-DEFECTO** (TTL) | TTL | PDF que cambia seguido |
| #11 | `/cotizar` self-routing (1 Flash), SSE, caché exacta si `esCacheable`, funnel permanente | **HEREDADA** iter. 4–10 | Caché semántica; history en la clave; clasificador (no reabrir) | Tokens / parafraseo |
| Cupos | 3 **prefijos** + caché `analyze:`/`chat:` + `funnel:` en el mismo KV `CHAT_LIMITS` | **HEREDADA** | KV aparte | Si un cupo queda muerto y otro explota |
| 502 Gemini | `/analizar` 502 **estructurado** + banner; `/cotizar` 502; chat 200+texto. Cupo ANALYZE se cobra igual. Funnel no marca 502 | **HEREDADA** (catch + iter. 502) | Retry 1×; no cobrar si falla | Si 66461-like se repite |
| Métricas Dashboard | Vistas SQL + fallback TS; conversión 30d (falla suave) | **HEREDADA** iter. 9 + 11 | Materializar KPIs | Timeout `v_kpis_dashboard` |

### Discrepancias código vs PLAN

1. Reranker: PLAN `bge-reranker-v2-m3` · código `@cf/baai/bge-reranker-base`.  
2. Chunks: PLAN 200–400 **con solape** · código 800/500 **sin** overlap.  
3. G1 GC: PLAN borra chunks al cerrar · cron **sin** `--gc`.  
4. Eval 63%: PLAN habla del Worker completo · G4 **sin** reranker.  
5. Caché de `/cotizar`: PLAN Fase 7 opcional pedía caché semántico. Hay caché **exacta** `chat:{id}:{pdf_hash}:{sha256}` solo si `esCacheable` (iter. 8). No hay embeddings de query ni parafraseo. El clasificador híbrido de iter. 8 **se retiró** en iter. 10 (self-routing Δ1).  
6. Drop `embedding(768)` + ivfflat (PLAN Fase 7): **no** hecho; v1 sigue en BD, chat no lo usa.  
7. Header contextual en **todos** los chunks (PLAN Fase 4): PDF embebe **cuerpo**; API sigue con header largo.  
8. **#9 postulabilidad:** criterio de acción diaria **sí** está en el ranking default (`esPostulable` + chip). El universo SQL sigue trayendo En Evaluación; el chip los muestra.

### Commits de referencia (corte 29 ago 2026)

| Repo | HEAD | Qué fija |
|---|---|---|
| monitor | `a060d2a` + este paquete de docs | Funnel SQL + reconciliar + docs 1–11. `vista_kpis_conversion.sql` entra en este commit |
| worker | `fdcc7fd` · CF `fcefa7a0-e623-432b-8479-71b576927cda` | Self-routing (`91e8484`) + funnel (`d1832cc`) + cleanup clasificador |
| web | `6f1a2f9` | Dashboard conversión 30d (encima de iter. 9 + 502) |

Iteraciones 1–11: ver [CHANGELOG_ITERACIONES.md](./CHANGELOG_ITERACIONES.md). Ancla histórica 20 ago: worker `c8113ae` · web `c0beff4` · CF `075d03be…`.

Snapshot: 29 ago 2026 (Perú).
