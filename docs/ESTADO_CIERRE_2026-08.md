# Estado de cierre — agosto 2026

Foto para retomar el proyecto sin rearmar contexto. Corte: **20 ago 2026** (Perú).
Detalle de cómo/por qué: `docs/ARQUITECTURA_TECNICA.md`.

Este archivo describe el **estado real en producción hoy** (iteraciones 1–7 del asesor ya desplegadas). No es un diario de sprints.

---

## Qué quedó en producción

Asesor completo (análisis 2º orden + chat conversacional) + pipeline + RAG v2 + login.

| Pieza | URL | Commit / versión viva |
|---|---|---|
| Front (GitHub Pages) | https://seace.rdiaz-lab.xyz | `9426dc1` (iteración 7) |
| Worker | https://seace-ai-proxy.rdiazg14.workers.dev | git `437c265` · CF `49953466-a6dd-4878-b549-8ebb5567bbcc` |
| Pipeline | cron 09:00 Perú (`0 14 * * *`) | monitor `e5f58d6` (backfill embed PDF) |

Flujos vivos:

- **#9 Ruta del día** — `/ruta-dia`, score 0–100 sin IA.
- **#10 Análisis** — `/analisis/:id` → `POST /analizar`. Caché KV 3 d (`analyze:{id}:{hash}`). Schema ampliado: 5 secciones base + timeline, viabilidad (ratio, componentes, contradicciones), alternativas[], chips, consorcio tri-estado. 422 `sin_tdr` si TDR &lt; 200 chars.
- **#11 Cotización asistida** — panel lateral en la misma página → `POST /cotizar`. Lee caché #10; 409 si no hay análisis. Clasificador Flash (nivel/formato/internet) + generate JSON. SSE de presentación si `Accept: text/event-stream`. Fail-closed sin `supuestos_aplicados`.
- **Chat RAG v2** — `POST /` con `backend=v2` (Gemini embed 1536 + HNSW + FTS + RRF + reranker-base + Flash). SSE real del modelo.
- **#3 Memoria de chat** — `history[]` (máx 8×500). Worker stateless.
- **Login** — `signInWithPassword`; signup público cerrado.
- **Pipeline diario** — ingesta → G1 (sin `--gc`) → detalle → PDF nativo → OCR selectivo 2 h → chunk → embed `embedding_v2`.

---

## Producto asesor (iteraciones 1–7) — qué hay hoy

Cada fila es capacidad **en prod**, no un backlog.

| # | Capacidad | Endpoint / UI | Archivos | Schema / contrato | Commits (prod) |
|---|---|---|---|---|---|
| 1 | Razonamiento 2º orden en el análisis | `POST /analizar` | Worker `src/analizar.ts` | `timeline.hitos[]` (`momento_dia`, `tiene_pago`, `es_critico`); `viabilidad.ratio_alcance` (min/max + techo); `viabilidad.cotizacion_por_componente[]`; `viabilidad.contradicciones_tdr[]`; `alternativas[]` (N variable, 1 `recomendada`) | Worker `75e84af`, `9573c5d` |
| 2 | UI estructural del análisis | `/analisis/:id` | Web `AnalisisV2.tsx`, `AnalisisContrato.tsx` | Infografía ratio; N alternativas dinámicas; economía por componente; bloque contradicciones | Web `4a6742f` |
| 2.5 | Coherencia económica + consorcio honesto | `POST /analizar` + UI | Worker `analizar.ts` (`alinearEconomiaConAlternativa`); web badge consorcio | `economia` = vía `recomendada`; `admite_consorcio` null si no consta; `optimizacion` tácticas de ejecución, no vías | Worker `0b58703`; web `f1263f9` |
| 3 | Timeline fishbone | `/analisis/:id` | Web `TimelineFishbone.tsx` | Thumbnail + fullscreen; eje escala con salto de plazo (`momento_dia`) | Web `8424cad` |
| 4 | Routing del chat (texto / tabla / gráfica) | `POST /cotizar` | Worker `cotizar.ts`, `escenario.ts`; web `ChatTable.tsx`, `ChatChart.tsx` | Clasificador `{nivel, formato, necesita_internet}`; schema condicional; post-proceso `completarEstructuras` si Gemini omite tabla/gráfica | Worker `28eaf4b` (+ `9f16f99`, `9884ae2`); web `e35de62` |
| 5 | Panel lateral atado al contrato | `/analisis/:id` | Web `AnalisisContrato.tsx` (`ChatEscenarios`) | `key={contratoId}`; persistencia `localStorage chat_escenarios_{id}`; FAB + panel 380 px | Web `534c1de` |
| 6 | Pulido visual y timeline | `/analizar` + UI | Worker `completarMomentoDia`; web tema gráficas | `momento_dia` siempre estimado (prompt + post-proceso); chips 3 niveles; CSS `--chart-1…6` | Worker `899372a`; web `301a62b` |
| 7 | UX conversacional | `POST /cotizar` + panel | Worker `cotizar.ts` (SSE); web `AnalisisContrato.tsx` | Panel abierto default desktop ≥1024 px; streaming de **presentación** del campo `escenario`; indicador Analizando→Analizado; andamiaje vacío oculto; prosa limpia en nivel 1 | Worker `437c265`; web `9426dc1` |

Contratos de calibración usados en esas iteraciones: **87880** SEDAPAR, **87502** CENEPRED.

---

## Cómo se protege el gasto

Tres sistemas de cupos **aislados** (claves KV distintas; un flujo no descuenta al otro):

| Sistema | RPM | RPD IP | Global día | Gemini por request |
|---|---|---|---|---|
| Chat RAG | `ip:` 8/60 | CHAT_RPD=40 | `flash:` FLASH_RPD=200 **Δ2** | extract + generate (embed **no** entra en Δ2) |
| `/analizar` | `analyze:ip:` 8/60 | ANALYZE_IP_RPD=15 | `analyze:` ANALYZE_RPD=40 | 1 generate si MISS; HIT=0 |
| `/cotizar` | `cotizar:ip:` 8/60 | COTIZAR_IP_RPD=20 (1 pregunta) | `cotizar:` COTIZAR_RPD=80 **Δ2** | clasificador + generate; 409=0 |

Tope de facturación Gemini: **S/10/mes en AI Studio** (no está en el código; se opera en la consola). Números y 502: ver sección G de `ARQUITECTURA_TECNICA.md`.

**No hay** caché de respuestas de `/cotizar`. La única caché de contenido es `/analizar` (`analyze:{id}:{hash}`, TTL 3 d). Próxima iteración prevista: eficiencia de IA (no empezada).

---

## Limitación conocida de #3

Seguimiento **conceptual** sí («¿y el plazo?»). Referencia que exige re-recuperar (**«dame más de ESE contrato»**) no: embed/filtros/RAG ven solo la query actual. Reescribir la query con otra llamada Flash **no está**.

---

## Mejoras pendientes (opcionales; ninguna bloquea el asesor)

- **Eficiencia de IA** — siguiente iteración de producto. Clasificador siempre llama Flash; `/cotizar` no cachea; no hay normalización de query ni métricas de repetición. Ver auditoría en el chat de 20 ago 2026.
- **#4 chunking** — 800/500 sin overlap es POR-DEFECTO. Experimento: overlap + tamaño vs baseline **63% success@10**.
- **Fase 7** — dropear `embedding(768)` + ivfflat; chat ya no los usa.
- **#12** — brief diario automático.
- Menores: Dashboard `d<=today`; Buscador doble badge; posible jubilar Dashboard/Buscador cuando Ruta del día sea el home.

---

## Punto de entrada para reevaluar

Lista **POR-DEFECTO** de `ARQUITECTURA_TECNICA.md` (empieza por ahí):

- reranker `bge-reranker-base` (PLAN pedía v2-m3)
- threshold vector **0.20**
- RRF **k=60**
- chunk **800/500 sin overlap** → medición = Tarea #4 vs 63%
- clasificador `/cotizar` = 1 Flash extra (no hay pre-filtro de reglas antes de Gemini)
- TTL caché #10 = 3 d (sin eval)
