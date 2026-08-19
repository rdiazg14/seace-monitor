# Estado de cierre — agosto 2026

Foto para retomar el proyecto sin rearmar contexto. Corte: **18–19 ago 2026** (Perú).
Detalle de cómo/por qué: `docs/ARQUITECTURA_TECNICA.md`.

---

## Qué quedó en producción

Asesor completo + pipeline + RAG v2 + login.

| Pieza | URL | Commit / versión viva |
|---|---|---|
| Front (Pages) | https://seace.rdiaz-lab.xyz | `d42b40f` (github-pages deploy `5975444317`) |
| Worker | https://seace-ai-proxy.rdiazg14.workers.dev | `ccb3a082` = git `8e23992` (`POST /cotizar`) |
| Pipeline | cron 09:00 Perú (`0 14 * * *`) | monitor `e1c5881` (arquitectura) + SQL/scripts de cierre |

Flujos vivos:

- **#9 Ruta del día** — `/ruta-dia`, score 0–100 sin IA.
- **#10 Análisis** — `/analisis/:id` → `POST /analizar` (caché KV 3 d, 422 `sin_tdr` si &lt;200 chars).
- **#11 Cotización asistida** — chat de escenarios sobre el análisis congelado → `POST /cotizar` (lee caché #10; 409 si no hay análisis; fail-closed sin `supuestos_aplicados`).
- **Chat RAG v2** — `POST /` con `backend=v2` (Gemini embed 1536 + HNSW + FTS + RRF + reranker-base + Flash).
- **#3 Memoria de chat** — `history[]` (máx 8×500). El Worker es stateless; el bloque `Conversación reciente` entra al prompt **después** del retrieve.
- **Login** — `signInWithPassword`; signup público cerrado. Altas por admin.
- **Pipeline diario** — ingesta → G1 (sin `--gc`) → detalle → PDF nativo → OCR selectivo 2 h → chunk → embed `embedding_v2`.

---

## Cómo se protege el gasto

Tres sistemas de cupos **aislados** (claves KV distintas; un flujo no descuenta al otro):

| Sistema | RPM | RPD IP | Global día | Gemini |
|---|---|---|---|---|
| Chat RAG | `ip:` 8/60 | CHAT_RPD=40 | `flash:` FLASH_RPD=200 **Δ2** | extract + generate |
| `/analizar` | `analyze:ip:` 8/60 | ANALYZE_IP_RPD=15 | `analyze:` ANALYZE_RPD=40 | 1 generate si MISS |
| `/cotizar` | `cotizar:ip:` 8/60 | COTIZAR_IP_RPD=20 | `cotizar:` COTIZAR_RPD=80 | 1 generate |

Tope de facturación Gemini: **S/10/mes en AI Studio** (no está en el código; se opera en la consola). Números y 502: ver sección G de `ARQUITECTURA_TECNICA.md`.

---

## Limitación conocida de #3

Seguimiento **conceptual** sí («¿y el plazo?»). Referencia que exige re-recuperar (**«dame más de ESE contrato»**) no: embed/filtros/RAG ven solo la query actual. Reescribir la query con otra llamada Flash **no está en v1**.

---

## Mejoras pendientes (opcionales; ninguna bloquea)

- **#4 chunking** — fruta baja. 800/500 sin overlap es POR-DEFECTO. Experimento ya diseñado: overlap + tamaño, eval offline contra baseline **63% success@10**.
- **Fase 7** — dropear `embedding(768)` + ivfflat; chat ya no los usa.
- **#12** — brief diario automático.
- Menores: 502 de Gemini en `/analizar` y `/cotizar` → mensaje amable (hoy JSON crudo); Dashboard `d<=today`; Buscador doble badge; posible jubilar Dashboard/Buscador cuando Ruta del día sea el home.

---

## Punto de entrada para reevaluar

Lista **POR-DEFECTO** de `ARQUITECTURA_TECNICA.md` (empieza por ahí):

- reranker `bge-reranker-base` (PLAN pedía v2-m3)
- threshold vector **0.20**
- RRF **k=60**
- chunk **800/500 sin overlap** → medición = Tarea #4 vs 63%
