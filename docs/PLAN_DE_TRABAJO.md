> **Estado de producto (20 ago 2026 noche):** el asesor #9/#10/#11 con iteraciones **1–9 + fixes** está en prod. Punto de entrada: [TRASPASO_MAESTRO_SEACE.md](./TRASPASO_MAESTRO_SEACE.md). Foto: [ESTADO_CIERRE_2026-08-20.md](./ESTADO_CIERRE_2026-08-20.md). Changelog: [CHANGELOG_ITERACIONES.md](./CHANGELOG_ITERACIONES.md). Este PLAN sigue siendo el de **retrieval v2 + pipeline**; varias filas (reranker v2-m3, chunks 200–400, costo $0/mes, checklist Gemini, «sin caché de chat») están **desactualizadas**. Si código y PLAN divergen, gana el código.

# PLAN DE TRABAJO — SEACE Monitor v2

> Consolidación completa de mejoras acordadas. Objetivo: convertir el prototipo funcional
> en un sistema **durable** (fresco, confiable y medible), no en un demo que se prueba una vez.
> Todo el stack corre en **free tier — costo objetivo: $0/mes**.

---

## 1. Objetivo

Que el monitor de contrataciones del SEACE:
1. **No mienta** sobre qué está vigente (frescura de estado real).
2. **Encuentre** lo que el usuario busca en español (retrieval de calidad).
3. **Contenga todo el contenido** de cada convocatoria, incluido el PDF del TDR — no solo el título.
4. **Avise cuando algo se rompe** en vez de degradarse en silencio.
5. **Se pueda medir** para saber si un cambio mejora o empeora.

---

## 2. Stack tecnológico definitivo

| Capa | Modelo / herramienta | Free tier | Si se pagara |
|---|---|---|---|
| LLM (generación) | **Gemini Flash 3.x** (thinking bajo) | ✅ 1,500/día | ~$1.50/$7.50 por 1M |
| Embeddings | **gemini-embedding-001 @ 1536 dims** | ✅ (rate-limited) | $0.15/M ($0.075 batch) |
| Reranker | **bge-reranker-v2-m3** (Cloudflare Workers AI) | ✅ | — |
| PDF → texto | **PyMuPDF** (extracción nativa) | ✅ | — |
| OCR (fallback escaneados) | **Gemini Flash visión** | ✅ | tokens |
| Base de datos | **Supabase** (Postgres + pgvector) | ✅ | — |
| Scraper + pipeline | **GitHub Actions** | ✅ | — |
| Worker / API | **Cloudflare Workers** | ✅ | — |

**Credenciales:** API key de Gemini guardada como secret `GEMINI_API_KEY` en Cloudflare (Worker) y en GitHub Actions. Mismo nombre en ambos.

---

## 3. Decisiones cerradas (no reabrir)

| # | Decisión | Razón |
|---|---|---|
| D1 | Embeddings = **gemini-embedding-001**, no bge | #1 MTEB multilingüe; bge-base-en era inglés sobre español |
| D2 | Dimensión = **1536** | pgvector solo indexa hasta 2000 dims con HNSW/ivfflat; 1536 = máxima calidad Matryoshka indexable |
| D3 | Índice = **HNSW**, no ivfflat | ivfflat lists=100 estaba mal dimensionado para ~9k filas (escaneaba ~1% con probes=1) |
| D4 | LLM = **Gemini Flash** fuera de Cloudflare | Llama 70B era caro en neurons, español regular; sacarlo libera el presupuesto de neurons de CF |
| D5 | Migración = **expand-contract** | Agregar columna nueva en paralelo, migrar, y recién al final limpiar; el RAG nunca queda a medias |
| D6 | Retrieval = **híbrido con RRF**, no fallback | El "si <3 chunks usa FTS" era un parche; RRF fusiona vector + léxico siempre |
| D7 | Descargar PDF = **siempre** (por vigente no procesado) | A veces son imágenes; hay que bajarlo para saberlo |
| D8 | Al RAG entra **todo el contenido** | Descripción + ítems CUBSO + cronograma + texto completo del TDR, no solo el título |

---

## 4. Diagnóstico — las 18 mejoras acordadas (nada fuera)

Marcadas por capa. Todas nacen de esta conversación y todas entran al plan.

**Datos / ingesta**
1. **Bug de frescura (crítico):** la ingesta incremental por `MAX(id)` nunca re-lee contratos existentes → los `Vigente` no se cierran cuando pasan a `Culminado`. El monitor se degrada en silencio. → *Fase G1*
2. **Sin recolección de basura:** los chunks de contratos ya cerrados quedan en la tabla vectorial para siempre. → *Fase G1*
3. **Sin validación de esquema:** si el SEACE cambia un campo, entra basura o revienta sin aviso. → *Fase G2 (dead-letter)*

**Contenido / PDF**
4. **Sin PDF/OCR:** el TDR real (finalidad, alcance, perfil, entregables, penalidades) no se captura. → *Fase 3*
5. **Descargar siempre + OCR condicional:** bajar el PDF de todo vigente no procesado; si PyMuPDF no extrae texto (escaneado), fallback a OCR. → *Fase 3*
6. **Todo el contenido al RAG:** no solo el título — descripción, ítems CUBSO, cronograma y TDR completo, chunkeados. → *Fase 4*

**Retrieval**
7. **Embeddings en español:** migrar bge-base-en → gemini-embedding-001. → *Fase 5*
8. **Reranker:** agregar bge-reranker-v2-m3 (mayor salto de precisión por esfuerzo). → *Fase 6*
9. **Fusión RRF:** reemplazar el fallback vector→FTS por Reciprocal Rank Fusion. → *Fase 6*
10. **Chunking contextual:** anteponer entidad + objeto + descripción a cada chunk. → *Fase 4*
11. **Parseo de query robusto:** los filtros por regex son frágiles → extracción estructurada / metadata filtering. → *Fase 6*

**Infraestructura del retrieval**
12. **Índice HNSW** en vez de ivfflat mal dimensionado. → *Fase 1*
13. **Dimensión 1536** por el límite de pgvector. → *Fase 1*

**Modelo de generación**
14. **Gemini Flash** en vez de Llama 70B; thinking bajo; output conciso. → *Fase 6*

**Durabilidad (lo que lo hace útil, no demo)**
15. **Frescura:** job de refresco de estados + GC. → *Fase G1*
16. **Confiabilidad:** alerta cuando el scraper falle o traiga 0 resultados. → *Fase G3*
17. **Medición:** set de ~30 queries reales con ground truth (incluida "trabajos de contador", que hoy falla). → *Fase G4*
18. **Caché semántico:** queries repetidas no vuelven a pegarle al LLM. → *Fase 7 (opcional)*

---

## 5. Arquitectura objetivo

```
┌─────────────────────────────────────────────────────────────────┐
│  GITHUB ACTIONS (cron diario)                                    │
│                                                                   │
│  scraper ──► FASE 1: altas (MAX id, incremental)                 │
│              FASE 2: enriquecer detalle (vigentes)               │
│              FASE 3: descargar PDF ─► PyMuPDF ─► ¿texto?          │
│                                         └─no─► OCR (Gemini visión)│
│              FASE 4: chunking contextual (todo el contenido)     │
│              FASE 5: embeddings (gemini-embedding-001 @1536)      │
│              FASE G1: refresh estados + cerrar vigentes + GC      │
│              FASE G3: si 0 resultados/error ─► ALERTA             │
└──────────────────────────────┬───────────────────────────────────┘
                               │ UPSERT / DELETE
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│  SUPABASE (Postgres + pgvector)                                   │
│   contratos  (+ req_url, pdf_*, tdr_texto, pdf_hash)             │
│   chunks_tdr (+ fuente, embedding_v2 vector(1536), HNSW)         │
└──────────────────────────────┬───────────────────────────────────┘
                               │ RLS read-only
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│  CLOUDFLARE WORKER (proxy seguro)                                │
│   query ─► extraer filtros (estructurado)                        │
│         ─► embed query (Gemini)                                  │
│         ─► retrieval híbrido (vector + léxico, RRF)              │
│         ─► rerank (bge-reranker-v2-m3)                           │
│         ─► contexto ─► Gemini Flash (SSE) ─► respuesta           │
└──────────────────────────────┬───────────────────────────────────┘
                               ▼
                         FRONTEND (chat / buscador)
```

---

## 6. Cambios de schema (Fase expand — aditivo, no rompe nada)

```sql
-- contratos: tracking de PDF
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS req_url         TEXT;
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS pdf_descargado  BOOLEAN DEFAULT FALSE;
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS pdf_procesado   BOOLEAN DEFAULT FALSE;
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS pdf_es_imagen   BOOLEAN;      -- se usó OCR?
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS tdr_texto       TEXT;         -- respaldo del texto extraído
ALTER TABLE contratos ADD COLUMN IF NOT EXISTS pdf_hash        TEXT;         -- detectar cambios del documento

-- chunks_tdr: nueva columna de embeddings en paralelo (no se toca la vieja aún)
ALTER TABLE chunks_tdr ADD COLUMN IF NOT EXISTS fuente        TEXT DEFAULT 'api';  -- 'api' | 'pdf'
ALTER TABLE chunks_tdr ADD COLUMN IF NOT EXISTS embedding_v2  vector(1536);

-- índice HNSW sobre la nueva columna
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_v2_hnsw
  ON chunks_tdr USING hnsw (embedding_v2 vector_cosine_ops);
```

La columna vieja `embedding vector(768)` y su índice ivfflat **se dropean recién en la Fase 7**, cuando el cutover esté validado.

---

## 7. Roadmap por fases (orden diseñado para embeber UNA sola vez)

### Fase 1 — Schema v2 (aditivo) ← EMPEZAR AQUÍ
- Correr el DDL de la sección 6 en Supabase.
- No requiere el endpoint del PDF → se construye ya, sin bloquear.
- Archivo: nuevo `schema_rag_v2.sql`.

### Fase 2 — Capturar endpoint del PDF *(tarea del usuario, en paralelo)*
- Método idéntico a `descubrir_api_detalle.py`: abrir DevTools → Network, dar clic en "Descargar requerimiento", copiar la URL real de la request del PDF con headers/cookies.
- Es el único blocker externo del pipeline de PDF.

### Fase 3 — Ingesta de PDF (`descargar_requerimiento.py`)
- `SELECT` contratos `WHERE estado='Vigente' AND pdf_descargado=false`.
- **Descargar siempre** el binario (reutilizando cookies de Playwright).
- Extraer texto con **PyMuPDF**.
- Si el texto sale vacío/insuficiente (PDF escaneado) → **OCR con Gemini visión** → marcar `pdf_es_imagen=true`.
- Guardar `tdr_texto` + `pdf_hash`; marcar `pdf_descargado=true`.
- **Eliminar el binario** temporal (descarga → lee → guarda → borra).

### Fase 4 — Chunking contextual (`chunker_contratos.py` extendido)
- Re-chunkear **todo el contenido** por contrato vigente:
  - descripción + objeto + ítems CUBSO + cronograma (`fuente='api'`)
  - sesiones del TDR: finalidad, alcance, perfil, entregables, penalidades, plazos (`fuente='pdf'`)
- Anteponer a cada chunk un encabezado de contexto: `ENTIDAD | OBJETO | Nº CONTRATO`.
- Chunks de ~200–400 tokens con solape, no de 46.

### Fase 5 — Re-embed único (`generar_embeddings.py` → Gemini)
- Poblar `embedding_v2` con gemini-embedding-001 @ 1536 para **todos** los chunks (api + pdf) en una sola pasada.
- Repartir en lotes para respetar el rate limit del free tier.

### Fase 6 — Worker (cutover a Gemini + reranker + RRF)
- Embeber la query con Gemini.
- Retrieval híbrido (vector sobre `embedding_v2` + léxico) fusionado con **RRF**.
- **Rerank** top-K con bge-reranker-v2-m3 (Cloudflare).
- Extracción de filtros estructurada (reemplaza regex).
- Generación con **Gemini Flash** (SSE, thinking bajo, output conciso).
- Quitar Llama 70B → liberar neurons.

### Fase G1 — Frescura + cierre + GC (`refresh_estados.py`) [durabilidad]
- Re-consultar los ~18k contratos en estados no-terminales (`Vigente`, `En Evaluación`).
- Actualizar su estado real; **cerrar los que pasaron a `Culminado`/`Cancelado`**.
- **Al cerrar un contrato, borrar sus chunks** de `chunks_tdr`.
- Resultado: el RAG solo contiene lo que está abierto — chico, rápido, siempre fresco.
- Corrige el bug #1 (contratos vigentes que nunca se cerraban).

### Fase G2 — Validación de ingesta (dead-letter) [durabilidad]
- Validar cada registro del SEACE contra un esquema (pydantic).
- Los inválidos → tabla `ingesta_rechazados` para revisión, en vez de romper o corromper.

### Fase G3 — Alertas [durabilidad]
- Si una corrida de GitHub Actions falla o trae 0 contratos nuevos de forma anómala → notificación (issue automático / webhook).
- Evita la muerte silenciosa cuando el SEACE cambie su SPA.

### Fase G4 — Medición (`eval_retrieval.py`) [durabilidad]
- Set de ~30 queries reales con lo que se espera encontrar (incluye "trabajos de contador", "ciberseguridad", "equipos de cómputo", "cloud", etc.).
- Medir precisión/recall de retrieval antes y después de cada cambio.
- Es la red de seguridad que evita degradarse a ciegas.

### Fase 7 — Limpieza + caché (opcional)
- Dropear `embedding vector(768)` y el índice ivfflat viejo.
- (Opcional) Caché semántico de queries repetidas para ahorrar llamadas al LLM.

---

## 8. Checklist de setup

- [x] Crear API key de Gemini (Google AI Studio, nivel gratuito)
- [ ] Guardar `GEMINI_API_KEY` como Secret en Cloudflare (Compute → Worker → Settings → Variables and Secrets)
- [ ] Guardar `GEMINI_API_KEY` como secret en GitHub (Settings → Secrets and variables → Actions)
- [ ] Agregar `pymupdf` a `requirements.txt`
- [ ] Capturar el endpoint de descarga del PDF (Fase 2)

---

## 9. Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| El SEACE cambia su SPA y el scraper muere | Fase G3 (alerta por 0 resultados/fallo) |
| Rate limit del free tier de Gemini al re-embeder 9k+ chunks | Fase 5 en lotes; no todo de golpe |
| PDFs escaneados sin texto | Fase 3 con fallback OCR (Gemini visión) |
| Migración deja el RAG a medias | Expand-contract (D5): la columna vieja sigue viva hasta validar |
| Un cambio empeora el retrieval sin que se note | Fase G4 (eval con ground truth) |
| La API key se filtra | Siempre como Secret; nunca en código ni en el repo; revocable desde AI Studio |

---

## 10. Métricas de éxito

- **Frescura:** 0 contratos marcados `Vigente` que en el SEACE ya estén `Culminado`.
- **Cobertura:** 100% de vigentes con TDR procesado (texto o OCR) e indexado.
- **Retrieval:** las 30 queries del set de evaluación devuelven resultados relevantes (incluida "contador").
- **Costo:** se mantiene en $0/mes.
- **Confiabilidad:** toda corrida fallida genera alerta.

---

*Documento vivo. Actualizar a medida que se cierren fases.*
