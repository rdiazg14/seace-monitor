# Arquitectura de datos por capas

> **Fase 4 (dual-write) aplicada:** la ingesta no manda `categoria_it` /
> `relevancia_ia` en el upsert. Escritores → `clasificacion_contrato`;
> trigger `trg_clasificacion_echo` copia a `contratos`. Keywords no pisan
> gemini/humano. `--forzar-completa` ya no pisa C1 (guard `SEACE_FORZAR_COMPLETA` retirado).

**Estado:** **Fases 0–4 aplicadas**. Lectores siguen en `contratos` (eco). **Fases 5–6** (migrar lectores / DROP columnas): **no**.

**Principio:** cada capa escribe solo sus tablas. La ingesta upserta hechos SEACE en `contratos` (sin `categoria_it`/`relevancia_ia`) y, para altas con keyword, escribe capa 3; el eco mantiene `contratos` sincronizado para los lectores.

**Corpus de referencia (corte previo a este diseño):** ~77 963 contratos; **4 213** con `categoria_it`; ~73 746 con ambas etiquetas NULL.

---

## 1. Por qué se parte `contratos`

`contratos` es a la vez:

| Qué | Ejemplos | Quién debería escribir |
|---|---|---|
| Hecho SEACE | título, entidad, fechas, estado | Ingesta / frescura / detalle |
| Blob opaco | `items_json` | Detalle |
| Data lake PDF | `pdf_*`, `tdr_texto` | Descarga / OCR / Storage |
| Inferencia de producto | `categoria_it`, `relevancia_ia` | Pipeline de clasificación, nunca el browser ni la ingesta |

El front, las vistas `v_contratos_estado` / `v_kpis_*`, `buscar_contratos` y el Worker leen las etiquetas como si fueran un hecho. Por eso el plan es **expand-contract**: primero existe la capa 3, luego se migran lectores, al final se borran las columnas de inferencia en `contratos`.

```
Capa 1  DECLARADO     ingesta, detalle, PDF          → contratos + contrato_items + documentos
Capa 2  CATÁLOGO      mantenimiento anual            → cubso_catalogo, cubso_version, it_keywords
Capa 3  INFERENCIA    pipeline, nunca el browser     → clasificacion_contrato + keyword_candidatas
Capa 4  ANÁLISIS/RAG  Worker + chunker               → analisis_contrato + chunks_tdr + cotizar_tipo_log
```

Regla de escritura (invariante a imponer en código, no en RLS):

- Ingesta escribe capa 3 solo en altas nuevas con keyword (`capa='keyword'`); no pisa gemini/humano.
- Keywords **no** pisan una fila con `capa IN ('gemini','humano')`.
- Gemini **no** pisa `capa = 'humano'`.
- El JWT admin **no** hace UPDATE de clasificación ni de `it_keywords` (RLS de solo lectura). Escritura admin: Worker con service_role (§11.5).
- Vocabulario: el sistema **activa solo** variantes (tipo A) y términos nuevos chicos y no colisionantes (tipo B bajo umbral). El admin puede revisar; no es un paso del flujo (§11).

---

## 2. Decisiones (antes del DDL)

### 2.1 `clasificacion_historial` — no en v1

El mismo shape sin PK, append-only, **no se crea ahora**.

Motivo: ya perdimos historia una vez (C2 desetiquetó ~222 Hardware) y la red fue `categoria_it_snapshot_c2`, no un event store. Un historial suelto se llena con no-ops cada vez que un backfill reescribe la misma categoría. Para no perder C1 hoy basta:

1. Snapshot de fase 0 (copia de las 4 213 + filas solo-IA) **antes** de cualquier backfill.
2. Una fila vigente en `clasificacion_contrato`.
3. Artefactos de disco que ya existen (`data/consenso_it_*.json`, ledger de rechazadas).

**Cuándo sí conviene:** el día que Gemini corra en cron (C4) y pise/confirme a diario. Entonces una tabla `clasificacion_historial` **append-on-change** (trigger `IS DISTINCT FROM` sobre `(categoria_it, relevancia_ia, capa)`), con `id bigserial` + `capturado_utc`. No el mismo shape “sin PK”: sin `id` no se puede auditar ni borrar basura.

No es event-sourcing. No se versiona cada corrida.

### 2.2 `analisis_contrato` — JSONB único + columnas de clave

El payload de `/analizar` es `AnalisisPayload` en `seace-web/src/lib/analisis.ts`: árbol anidado (encaje, condiciones, economía, veredicto, timeline, alternativas, viabilidad, cláusulas…). El schema **cambia con el prompt**. Columnas por campo = una migración DDL por cada iteración del prompt.

Hoy el JSON vive en KV (`analyze:{id}:{pdf_hash}`, TTL 3 días). Postgres es la caché durable.

**Diseño:**

| Columna | Para qué |
|---|---|
| `contrato_id`, `pdf_hash` | PK. Misma clave que el Worker. Hash vacío → `'na'` (igual que `cacheKey`). |
| `prompt_version` | Invalidar: si cambia el prompt, el Worker **actualiza** la fila (no historial de análisis). |
| `modelo`, `tdr_fuente`, `tdr_chars` | Observabilidad; ya viajan en `AnalisisResponse`. |
| `payload jsonb` | El `AnalisisPayload` completo. |

No se extraen `veredicto.codigo` ni `encaje.rubro` a columnas hasta que un KPI de Dashboard los consulte por SQL. Hoy no lo hacen: el análisis se pide al Worker.

### 2.3 `cotizaciones` — no reemplaza `cotizar_tipo_log`

`cotizar_tipo_log` es un **log de eventos** (`tipo_respuesta`, `categoria_it`, `created_at`) que Observabilidad ya lee. El JSON de cotizar sigue en KV. No son el mismo objeto.

Reemplazarlo ahora obliga a backfill imposible (KV con TTL) y rompe `/observabilidad`. Se deja. Una tabla `cotizacion_contrato` (espejo de `analisis_contrato`) se abre **solo** cuando se quiera persistir el `EscenarioPayload` más de 3 días. No es esta migración.

Los flags `analizado` / `cotizado` / `fecha_*` se quedan en `contratos` (funnel #10/#11). Moverlos a “existe fila en analisis_contrato” es un segundo contrato, no este.

### 2.4 FK `contrato_items` → `cubso_catalogo`

**No hay FK de base.** El catálogo se actualiza desde el XLSX vigente del OECE
(`scripts/cargar_cubso.py`). Tras la carga del **02-jul-2026**, de los 4 213
`cod_cubso` distintos en `contrato_items` solo **30** faltan en
`cubso_catalogo` (antes, con el dump 2016, faltaban 1 772). El upsert por
`codigo` agrega y actualiza sin borrar históricos. El join es
`LEFT JOIN cubso_catalogo c ON c.codigo = i.cod_cubso`. Índice en `cod_cubso`
para ese join.

### 2.5 `capa` en las 4 213

| Origen | `capa` | Cómo se distingue |
|---|---|---|
| 54 ids C1 (`IDS_C1_HARDCODE`) | `gemini` | `consenso_n >= 1`, `revisar = false`, `artefacto = 'c1_consenso'` |
| `90331` | `gemini` | `consenso_n = 0`, `revisar = true`, `artefacto = 'gemini_directo_2026-09-01'` |
| Resto con etiqueta | `keyword` | `keyword_id` NULL (no se reconstruye qué keyword ganó) |
| Futuro override admin | `humano` | 0 filas hoy |
| Futuro clasificador CUBSO | `cubso` | 0 filas hoy; **no** se rellena con segmento 43/81 |

C1 no es `humano`: un humano **aplicó** el consenso, el modelo **propuso**. `humano` se reserva para C3 (edición explícita en UI).

`capa` describe **de dónde salió `categoria_it`**. `relevancia_ia` sigue siendo de keywords (Gemini C1 no la escribe). Una fila C1 puede tener categoría Gemini y relevancia de ingesta; no se inventa `capa_relevancia`.

---

## 3. Mapa de tablas

### Capa 1 — Declarado (solo ingesta / detalle / PDF)

**`contratos` (existente).** Se le quitan al final `categoria_it` y `relevancia_ia`. Se quedan los hechos SEACE, frescura, funnel, y —hasta un contract posterior— `items_json` y `pdf_*` como duplicado de las tablas nuevas.

**`contrato_items` (nueva).** Normaliza `items_json`. Motivo: hoy el JSON es opaco; con filas se puede `JOIN cubso_catalogo` y filtrar por segmento/familia.

**`documentos` (nueva).** Un contrato puede tener más de un anexo. Hoy `pdf_archivo_id` / `pdf_storage_path` / `pdf_hash` son columnas 1:1 en `contratos`.

No se mueven en v1 (siguen en `contratos`): `tdr_texto` (caché para Gemini), `paginas_ocr_pendientes` / `paginas_ocr_hechas` (cola de OCR, no data lake), `analizado` / `cotizado`.

### Capa 2 — Catálogo (sin cambios)

`cubso_catalogo`, `cubso_version`, `it_keywords`. Mantenimiento anual / C3 keywords. Escritura: service_role. Lectura admin: `es_admin()`.

### Capa 3 — Inferencia

**`clasificacion_contrato`.** Una vigente por contrato (PK `contrato_id`).

**`keyword_candidatas`.** Gemini copia una señal literal que las keywords no vieron (ej. título truncado `"desarro"`). Evaluación automática tipo A/B (§11). El admin es opcional.

### Capa 4 — Análisis y RAG

**`analisis_contrato` (nueva).** JSON de `/analizar`.

**`chunks_tdr`.** Sin cambios.

**`cotizar_tipo_log`.** Sin cambios. No se crea `cotizaciones`.

---

## 4. DDL propuesto

> Fase 0–3: SQL en `docs/capas_fase*.sql` (aplicados). El SQL de esta sección es la referencia; si diverge, gana el archivo en `docs/`. No hace DROP de columnas de `contratos`. Fases 4–6 **no** aplicadas.

Convención RLS (la de prod hoy):

- `ENABLE ROW LEVEL SECURITY`.
- `service_role` bypasea RLS (escritura pipeline / Worker).
- Anon/authenticated: solo `SELECT` donde el producto ya lee en público.
- Tablas internas (candidatas, historial futuro): `SELECT` con `es_admin()`, igual que `it_keywords`.
- Ninguna política `INSERT`/`UPDATE`/`DELETE` para `anon`/`authenticated`. El JWT admin **no** escribe clasificación.

Tras aplicar (cuando se apruebe): `NOTIFY pgrst, 'reload schema';`

### 4.1 `contrato_items`

```sql
CREATE TABLE IF NOT EXISTS public.contrato_items (
  contrato_id  bigint  NOT NULL REFERENCES public.contratos(id) ON DELETE CASCADE,
  item_nro     int     NOT NULL,
  cod_cubso    text,
  nom_cubso    text,
  descripcion  text,
  cantidad     numeric,
  unidad       text,
  distrito     text,
  PRIMARY KEY (contrato_id, item_nro)
);

COMMENT ON TABLE public.contrato_items IS
  'Capa 1. Ítems CUBSO declarados (antes items_json). Sin FK a cubso_catalogo: el dump 2016 no cubre todos los códigos SEACE.';

COMMENT ON COLUMN public.contrato_items.item_nro IS
  'Orden 1-based en uitContratoItemProjectionList.';

CREATE INDEX IF NOT EXISTS contrato_items_cod_cubso_idx
  ON public.contrato_items (cod_cubso)
  WHERE cod_cubso IS NOT NULL;

CREATE INDEX IF NOT EXISTS contrato_items_contrato_idx
  ON public.contrato_items (contrato_id);

ALTER TABLE public.contrato_items ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON TABLE public.contrato_items TO anon, authenticated;

DROP POLICY IF EXISTS contrato_items_select_public ON public.contrato_items;
CREATE POLICY contrato_items_select_public
  ON public.contrato_items
  FOR SELECT TO anon, authenticated
  USING (true);
```

### 4.2 `documentos`

Valores de `tipo_extraccion` pedidos: `nativo` | `ocr` | `mixto`. El campo actual `contratos.tdr_tipo_extraccion` usa `nativo_puro` | `mixto` | `imagen_total`. Mapeo en backfill: `nativo_puro→nativo`, `mixto→mixto`, `imagen_total→ocr`. Sin fila si `req_url` es el marcador `sin_pdf` o no hay `pdf_archivo_id`.

```sql
CREATE TABLE IF NOT EXISTS public.documentos (
  id              bigserial PRIMARY KEY,
  contrato_id     bigint NOT NULL REFERENCES public.contratos(id) ON DELETE CASCADE,
  pdf_archivo_id  bigint,
  storage_path    text,
  sha256          text,
  bytes           bigint,
  paginas         int,
  tipo_extraccion text
                    CHECK (tipo_extraccion IS NULL
                           OR tipo_extraccion IN ('nativo', 'ocr', 'mixto')),
  descargado_utc  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (contrato_id, pdf_archivo_id)
);

COMMENT ON TABLE public.documentos IS
  'Capa 1. Data lake de anexos PDF. Un contrato puede tener más de un anexo; hoy la ficha solo persiste uno.';

COMMENT ON COLUMN public.documentos.sha256 IS
  'Equivalente a contratos.pdf_hash (hash del binario).';

COMMENT ON COLUMN public.documentos.storage_path IS
  'Ruta en bucket tdr: tdr/{contrato_id}/{pdf_archivo_id}.pdf.';

CREATE INDEX IF NOT EXISTS documentos_contrato_idx
  ON public.documentos (contrato_id);

CREATE INDEX IF NOT EXISTS documentos_sha256_idx
  ON public.documentos (sha256)
  WHERE sha256 IS NOT NULL;

ALTER TABLE public.documentos ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON TABLE public.documentos TO anon, authenticated;

DROP POLICY IF EXISTS documentos_select_public ON public.documentos;
CREATE POLICY documentos_select_public
  ON public.documentos
  FOR SELECT TO anon, authenticated
  USING (true);
```

No se indexa `storage_path` único: el path se deriva de ids.

### 4.3 `clasificacion_contrato`

```sql
CREATE TABLE IF NOT EXISTS public.clasificacion_contrato (
  contrato_id      bigint PRIMARY KEY
                     REFERENCES public.contratos(id) ON DELETE CASCADE,
  categoria_it     text,
  relevancia_ia    text,
  capa             text NOT NULL
                     CHECK (capa IN ('keyword', 'gemini', 'cubso', 'humano')),
  keyword_id       bigint
                     REFERENCES public.it_keywords(id) ON DELETE SET NULL,
  senal            text,
  senal_fuente     text,
  confianza        numeric,
  consenso_n       int NOT NULL DEFAULT 0,
  revisar          boolean NOT NULL DEFAULT false,
  artefacto        text,
  creado_utc       timestamptz NOT NULL DEFAULT now(),
  actualizado_utc  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT clasificacion_keyword_id_chk
    CHECK (capa <> 'keyword' OR true),
  CONSTRAINT clasificacion_tiene_etiqueta_chk
    CHECK (categoria_it IS NOT NULL OR relevancia_ia IS NOT NULL)
);

COMMENT ON TABLE public.clasificacion_contrato IS
  'Capa 3. Una clasificación vigente por contrato. Escritura: pipeline. Nunca el browser ni la ingesta.';

COMMENT ON COLUMN public.clasificacion_contrato.capa IS
  'Origen de categoria_it. relevancia_ia es de keywords salvo override futuro.';

COMMENT ON COLUMN public.clasificacion_contrato.keyword_id IS
  'Solo informativo si capa=keyword y se conoce la regla ganadora. Histórico 4213: NULL.';

COMMENT ON COLUMN public.clasificacion_contrato.senal IS
  'Substring o título que disparó la etiqueta (Gemini lo copia literal).';

COMMENT ON COLUMN public.clasificacion_contrato.consenso_n IS
  'C1: corridas Gemini que coincidieron. Camino directo: 0.';

CREATE INDEX IF NOT EXISTS clasificacion_categoria_idx
  ON public.clasificacion_contrato (categoria_it)
  WHERE categoria_it IS NOT NULL;

CREATE INDEX IF NOT EXISTS clasificacion_ia_idx
  ON public.clasificacion_contrato (relevancia_ia)
  WHERE relevancia_ia IS NOT NULL;

CREATE INDEX IF NOT EXISTS clasificacion_capa_idx
  ON public.clasificacion_contrato (capa);

CREATE INDEX IF NOT EXISTS clasificacion_revisar_idx
  ON public.clasificacion_contrato (contrato_id)
  WHERE revisar;

ALTER TABLE public.clasificacion_contrato ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON TABLE public.clasificacion_contrato TO anon, authenticated;

DROP POLICY IF EXISTS clasificacion_select_public ON public.clasificacion_contrato;
CREATE POLICY clasificacion_select_public
  ON public.clasificacion_contrato
  FOR SELECT TO anon, authenticated
  USING (true);
```

`keyword_id` es nullable a propósito: el CHECK “si keyword entonces FK” no se puede exigir para el backfill histórico. El pipeline nuevo sí debería rellenarlo cuando gane una regla.

Trigger de `actualizado_utc` (opcional, fase 1):

```sql
CREATE OR REPLACE FUNCTION public.fn_clasificacion_touch()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.actualizado_utc := now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_clasificacion_touch ON public.clasificacion_contrato;
CREATE TRIGGER trg_clasificacion_touch
  BEFORE UPDATE ON public.clasificacion_contrato
  FOR EACH ROW EXECUTE FUNCTION public.fn_clasificacion_touch();
```

### 4.4 `keyword_candidatas`

Estados: `nueva` | `medida` | `auto_activada` | `aprobada_admin` | `rechazada`. Flujo y umbrales en §11. El SQL aplicado está en `docs/capas_fase1_tablas.sql`.

```sql
CREATE TABLE IF NOT EXISTS public.keyword_candidatas (
  id                    bigserial PRIMARY KEY,
  senal                 text NOT NULL,
  categoria_propuesta   text NOT NULL,
  veces_vista           int  NOT NULL DEFAULT 1,
  contratos             bigint[] NOT NULL DEFAULT '{}',
  ejemplo_contrato_id   bigint REFERENCES public.contratos(id) ON DELETE SET NULL,
  primera_vez_utc       timestamptz NOT NULL DEFAULT now(),
  ultima_vez_utc        timestamptz NOT NULL DEFAULT now(),
  estado                text NOT NULL DEFAULT 'nueva'
                          CHECK (estado IN (
                            'nueva', 'medida', 'auto_activada',
                            'aprobada_admin', 'rechazada'
                          )),
  tipo_eval             text
                          CHECK (tipo_eval IS NULL OR tipo_eval IN ('a', 'b')),
  keyword_madre_id      bigint REFERENCES public.it_keywords(id) ON DELETE SET NULL,
  keyword_id            bigint REFERENCES public.it_keywords(id) ON DELETE SET NULL,
  universo_a            int,
  cambios_categoria     int,
  ya_etiquetados        int,
  ratio_predictivo      numeric,
  evidencia             jsonb,
  evaluada_utc          timestamptz,
  activada_utc          timestamptz,
  activada_por          text,
  nota                  text,
  UNIQUE (senal, categoria_propuesta)
);

COMMENT ON TABLE public.keyword_candidatas IS
  'Capa 3. Señales que Gemini vio y it_keywords no. Tipo A/B se evalúan solas (§11). Nunca auto-activar por encima de UMBRAL_AUTO.';

CREATE INDEX IF NOT EXISTS keyword_candidatas_estado_idx
  ON public.keyword_candidatas (estado);

CREATE INDEX IF NOT EXISTS keyword_candidatas_contratos_gin
  ON public.keyword_candidatas USING GIN (contratos);

ALTER TABLE public.keyword_candidatas ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON TABLE public.keyword_candidatas TO authenticated;

DROP POLICY IF EXISTS keyword_candidatas_select_admin ON public.keyword_candidatas;
CREATE POLICY keyword_candidatas_select_admin
  ON public.keyword_candidatas
  FOR SELECT TO authenticated
  USING (public.es_admin());
```

`contratos bigint[]` aguanta la escala de C1/C4 (decenas–cientos de ids por señal). Tabla N:M cuando una candidata cruce ~1 000 contratos; no ahora.

### 4.5 `analisis_contrato`

```sql
CREATE TABLE IF NOT EXISTS public.analisis_contrato (
  contrato_id     bigint NOT NULL REFERENCES public.contratos(id) ON DELETE CASCADE,
  pdf_hash        text   NOT NULL DEFAULT 'na',
  prompt_version  text   NOT NULL,
  modelo          text,
  tdr_fuente      text,
  tdr_chars       int,
  payload         jsonb  NOT NULL,
  creado_utc      timestamptz NOT NULL DEFAULT now(),
  actualizado_utc timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (contrato_id, pdf_hash)
);

COMMENT ON TABLE public.analisis_contrato IS
  'Capa 4. Caché durable de POST /analizar. payload = AnalisisPayload. Invalidar subiendo prompt_version (UPDATE in-place).';

CREATE INDEX IF NOT EXISTS analisis_contrato_creado_idx
  ON public.analisis_contrato (creado_utc DESC);

ALTER TABLE public.analisis_contrato ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON TABLE public.analisis_contrato TO anon, authenticated;

DROP POLICY IF EXISTS analisis_select_public ON public.analisis_contrato;
CREATE POLICY analisis_select_public
  ON public.analisis_contrato
  FOR SELECT TO anon, authenticated
  USING (true);
```

Lectura pública: el front ya usa la anon key y pide `/analizar` sin login. Poner el JSON en PG no abre un canal nuevo; evita re-gastar Flash. Escritura: Worker con `service_role`.

### 4.6 Vista de compatibilidad (fase 5, no fase 1)

El front y PostgREST leen `contratos.categoria_it`. Hasta el DROP, la vía **cero ruptura de front** es un trigger de eco (fase 4) más esta vista para lectores nuevos:

```sql
CREATE OR REPLACE VIEW public.v_contratos AS
SELECT
  c.id,
  c.nro_contratacion,
  c.descripcion_contrato,
  c.objeto,
  c.descripcion,
  c.entidad,
  c.estado,
  c.fecha_publica,
  c.fecha_ini_cotizacion,
  c.fecha_fin_cotizacion,
  c.tipo_cotizacion,
  c.cotizar,
  cl.categoria_it,
  cl.relevancia_ia,
  c.nom_area_usuaria,
  c.items_json,
  c.detalle_cargado,
  c.pdf_archivo_id,
  c.pdf_storage_path,
  c.pdf_hash,
  c.tdr_texto,
  c.analizado,
  c.cotizado,
  c.fecha_analisis,
  c.fecha_cotizacion,
  cl.capa            AS clasificacion_capa,
  cl.revisar         AS clasificacion_revisar
FROM public.contratos c
LEFT JOIN public.clasificacion_contrato cl ON cl.contrato_id = c.id;

GRANT SELECT ON public.v_contratos TO anon, authenticated;
```

No es `SELECT *` a propósito: el día del DROP las columnas viejas dejan de existir y el `*` de una vista `contratos.*` + join se rompería si alguien la hubiera definido así.

`dashboard_resumen`, `vigentes_urgentes`, `buscar_contratos` y `v_contratos_estado` se reescriben en fase 5 para tomar `cl.categoria_it` / `cl.relevancia_ia` y el predicado IT:

```sql
-- predicado IT (reemplaza c.categoria_it IS NOT NULL OR c.relevancia_ia IS NOT NULL)
cl.contrato_id IS NOT NULL
-- equivalente: cl.categoria_it IS NOT NULL OR cl.relevancia_ia IS NOT NULL
-- (la tabla ya exige al menos una etiqueta)
```

### 4.7 Trigger de eco (fase 4, temporal)

Mantiene `contratos.categoria_it` / `relevancia_ia` iguales a la fila vigente **sin que la ingesta las escriba**. Se borra en el contract (fase 6).

```sql
CREATE OR REPLACE FUNCTION public.fn_clasificacion_echo()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  UPDATE public.contratos
     SET categoria_it  = NEW.categoria_it,
         relevancia_ia = NEW.relevancia_ia
   WHERE id = NEW.contrato_id;
  RETURN NEW;
END;
$$;

-- SOLO después de que la ingesta ya no mande esas columnas en el upsert
DROP TRIGGER IF EXISTS trg_clasificacion_echo ON public.clasificacion_contrato;
CREATE TRIGGER trg_clasificacion_echo
  AFTER INSERT OR UPDATE OF categoria_it, relevancia_ia
  ON public.clasificacion_contrato
  FOR EACH ROW EXECUTE FUNCTION public.fn_clasificacion_echo();
```

Sin este trigger, hay que cambiar Ruta del día, Buscador, Dashboard fallback, Chat, ficha de análisis y Worker **en el mismo deploy** que el corte de dual-write. Con trigger, el front sigue leyendo `contratos` hasta el DROP.

---

## 5. Consumidores: hoy → después

### 5.1 Ruta del día

**Hoy.** `RutaDia.tsx` pagina `contratos` con `RUTA_DIA_COLS` (incluye `categoria_it`, `relevancia_ia`, `pdf_archivo_id`, `pdf_storage_path`), estados `Vigente` | `En Evaluación`, filtro `.or('categoria_it.not.is.null,relevancia_ia.not.is.null')`, tope 20 000. Recorte postulable y score en cliente (`rutaDia.ts`).

**Después.** Misma query contra `v_contratos` (o `contratos` mientras viva el eco). El `.or(...)` pasa a `.not('categoria_it', 'is', null)` **o** se deja el OR (la vista expone ambas columnas). PDF: seguir leyendo columnas de `contratos` hasta que el front pida `documentos` (fase 7, opcional). No hace falta `contrato_items` en esta pantalla.

### 5.2 Dashboard

**Hoy.**

| Llamada | Fuente | Usa etiquetas |
|---|---|---|
| `cargarCapaSemantica()` | `v_kpis_dashboard`, `v_kpis_negocio`, `v_contratos_estado` (fallback: `contratos` + `IT_OR`) | Sí |
| `cargarKpisConversion()` | `v_kpis_conversion` / `_rubro` (JOIN `v_contratos_estado`) | Sí, vía la vista |
| `dashboard_resumen` | vista `GROUP BY categoria_it` | Sí |
| `contratos` select `*` limit 10 | recientes | Pinta `ItPill` si hay cat |
| `contratos.fecha_publica` limit 1 | “última alta” | No |

**Después.** Reescribir las cinco vistas SQL para JOIN a `clasificacion_contrato`. El TS de `capaSemantica.ts` no cambia de contrato si las vistas conservan nombres de columna. `dashboard_resumen` debe leer `cl.categoria_it`. Recientes: `v_contratos` o eco.

`fn_rubro_energetic(categoria_it, relevancia_ia, descripcion, …)` no cambia: recibe parámetros, no lee la tabla.

### 5.3 Buscador

**Hoy.**

1. Sort relevancia + término: RPC `buscar_contratos` (SECURITY DEFINER, columnas incluyen `categoria_it`, `relevancia_ia`). Filtro de chips IT **en cliente** sobre `r.categoria_it`. Segundo SELECT a `contratos` por `pdf_archivo_id, pdf_storage_path`.
2. Otro sort / sin término: `contratos.select('*')` + `.in('categoria_it', cats)` + ilike.

**Después.** Reescribir `buscar_contratos` con `LEFT JOIN clasificacion_contrato cl` y devolver `cl.categoria_it`. El `.in('categoria_it', cats)` sobre `contratos` **rompe** el día del DROP; en fase 5 hay que apuntarlo a `v_contratos` o filtrar `clasificacion_contrato`. PDF: igual que Ruta.

Sugerencias (`Buscador.tsx` ~L117): hoy leen `contratos`; no dependen de etiquetas.

### 5.4 Worker (`seace-ai-proxy`)

**Hoy.**

| Endpoint | Lee | Escribe |
|---|---|---|
| `GET /` chat | `contratos` (ficha con `categoria_it`), `chunks_tdr`, RPC `buscar_tdr_v2` / `buscar_contratos` | nada en PG |
| `POST /analizar` | ficha (`categoria_it`, `relevancia_ia`, `tdr_texto`, `pdf_hash`), chunks | KV `analyze:{id}:{hash}`; funnel KV |
| `POST /cotizar` | misma ficha | `cotizar_tipo_log` (copia `categoria_it`); KV |

**Después.** Ficha: `v_contratos` o join. `/analizar` además `INSERT/UPDATE analisis_contrato` (capa 4). `cotizar_tipo_log.categoria_it` se rellena desde `clasificacion_contrato` (denormalizado a propósito: es un evento histórico; no FK). `chunks_tdr` intacto. `buscar_contratos` debe devolver categoría post-JOIN o el RRF del chat pierde el chip IT.

### 5.5 Pipeline (no browser)

| Job | Hoy escribe en | Después |
|---|---|---|
| `ingesta_completa.py` | `contratos` **incluyendo** cat/ia | Solo hechos SEACE. **Prohibido** mandar `categoria_it` / `relevancia_ia` en el upsert |
| job keywords (nuevo, mismo cron) | — | `INSERT` clasificacion si no existe; `UPDATE` solo si `capa = 'keyword'` |
| `backfill_categoria.py` | `UPDATE contratos.categoria_it` (protege C1 / 90331 a mano) | `UPDATE clasificacion` con la misma guarda (`capa = 'keyword'`) |
| `clasificar_gemini.py` | `UPDATE contratos.categoria_it` si NULL/NULL | `INSERT/UPDATE clasificacion` `capa='gemini'`; no pisa `humano`; emite `keyword_candidatas` |
| `enriquecer_detalle.py` | `nom_area_usuaria`, `items_json`, `detalle_cargado` | + `contrato_items` (delete+insert por contrato) |
| `descargar_requerimiento.py` / `subir_pdf_storage.py` | columnas `pdf_*` / `tdr_*` | + `documentos` (upsert por `contrato_id, pdf_archivo_id`) |
| `chunker_contratos.py` | `chunks_tdr` | igual |
| frescura de estado | `estado`, `estado_verificado_at` | igual |

`--forzar-completa` vuelve a bajar el corpus y hace upsert masivo. Con la ingesta ciega a la capa 3, **deja de ser capaz de pisar C1**. Esa es la condición de éxito del diseño.

### 5.6 Observabilidad / auditorías

Observabilidad lee `cotizar_tipo_log` (admin RLS) y stats de pipeline. No depende de las columnas a borrar.

Scripts en `auditoria-clasificador/` leen `contratos.categoria_it`. Siguen funcionando con el eco; el día del DROP hay que JOIN. No es producción.

---

## 6. Migración de las 4 213 etiquetas

Fuente de verdad el día D (en este orden, ninguna pisa a la anterior):

1. Snapshot fase 0 (`categoria_it_snapshot_capas`) — foto de `contratos` **antes** del INSERT.
2. `IDS_C1_HARDCODE` (54 ids, `scripts/backfill_categoria.py`).
3. `90331` (`IDS_PROTEGIDOS`).
4. Resto: `capa = 'keyword'`.

No se usa `categoria_it_snapshot_c2` como origen de las etiquetas actuales (es la foto **pre-C2**, con Hardware que C2 quitó). Se conserva como red de C2; no se borra.

### 6.1 SQL de backfill (fase 2 — propuesto, no ejecutar)

Ids C1 (54), copiados del hardcode vigente:

`273, 10353, 11435, 11988, 12399, 20626, 32171, 32378, 34382, 34492, 35576, 35751, 36445, 36973, 40586, 43667, 46129, 50908, 55367, 57244, 57871, 57882, 58672, 59934, 63954, 65580, 65997, 66279, 67658, 68477, 70601, 70826, 72158, 72867, 74482, 77609, 77999, 79918, 84043, 85541, 88126, 90076, 90342, 90815, 90819, 90832, 90869, 90875, 90891, 91148, 91197, 91221, 91321, 91342`

```sql
-- Universo: toda fila que hoy alimenta el radar (cat O ia), no solo las 4213.
INSERT INTO public.clasificacion_contrato (
  contrato_id, categoria_it, relevancia_ia,
  capa, keyword_id, senal, senal_fuente,
  confianza, consenso_n, revisar, artefacto
)
SELECT
  c.id,
  c.categoria_it,
  c.relevancia_ia,
  CASE
    WHEN c.id = 90331 THEN 'gemini'
    WHEN c.id IN (/* 54 ids C1 */) THEN 'gemini'
    ELSE 'keyword'
  END,
  NULL,  -- keyword_id desconocido en histórico
  NULL,  -- senal no reconstruida
  NULL,
  NULL,
  CASE
    WHEN c.id IN (/* 54 ids C1 */) THEN 1  -- consenso aplicado; N exacto está en el JSON de disco
    ELSE 0
  END,
  (c.id = 90331),
  CASE
    WHEN c.id = 90331 THEN 'gemini_directo_2026-09-01'
    WHEN c.id IN (/* 54 */) THEN 'c1_consenso'
    ELSE 'backfill_keywords'
  END
FROM public.contratos c
WHERE c.categoria_it IS NOT NULL
   OR c.relevancia_ia IS NOT NULL
ON CONFLICT (contrato_id) DO NOTHING;
```

`keyword_id`, `senal`, `confianza`: NULL. Reconstruir la keyword ganadora sobre 4 213 es arqueología y puede equivocarse (C2 ya cambió reglas). El pipeline **nuevo** sí las llena en altas.

Filas solo-`relevancia_ia` (sin `categoria_it`) entran con `capa = 'keyword'`. Cuentan para el radar (`IT_OR`) y no están en el “4 213”. El CHECK exige al menos una etiqueta.

### 6.2 Qué se valida (fase 2)

| Check | Esperado |
|---|---|
| `COUNT(*) FROM clasificacion_contrato` | `COUNT(*) FROM contratos WHERE categoria_it IS NOT NULL OR relevancia_ia IS NOT NULL` |
| `COUNT(*) FILTER (WHERE categoria_it IS NOT NULL)` | **4 213** (o el número del snapshot del día D, no un hardcode viejo) |
| 54 ids C1 | `capa = 'gemini' AND revisar = false AND artefacto = 'c1_consenso'` |
| `90331` | `capa = 'gemini' AND revisar = true` |
| Esas 55 | ninguna `capa = 'keyword'` |
| Join de igualdad | `contratos.categoria_it IS NOT DISTINCT FROM cl.categoria_it` y lo mismo en `relevancia_ia` para todo el universo migrado = 0 desvíos |
| C1 vs snapshot fase 0 | las 54 coinciden con `categoria_it_antes` |

Si el COUNT de `categoria_it` el día D ≠ 4 213, gana el snapshot, no este documento.

### 6.3 `contrato_items` y `documentos` (fase 3)

Ítems: un `jsonb_array_elements` de `items_json` con `item_nro = ordinality`. Contratos sin `items_json` → 0 filas (correcto). Validar: nº contratos con items en JSON = nº `contrato_id` distintos en la tabla.

Documentos: una fila por contrato con `pdf_archivo_id IS NOT NULL` (o `pdf_descargado = true`). `sha256 = pdf_hash`, `storage_path = pdf_storage_path`, `paginas = tdr_n_paginas`, `tipo_extraccion` mapeado. `bytes` NULL (hoy no se persiste). Validar: COUNT documentos ≈ COUNT contratos con PDF; 0 duplicados `(contrato_id, pdf_archivo_id)`.

---

## 7. Plan expand-contract

Ninguna fase cambia el contrato del front salvo la 5 (vistas, mismo shape) y la 6 (DROP, con eco ya retirado y lectores en vista). **Producción no puede quedar sin `categoria_it` visible** entre 5 y 6.

### Fase 0 — Snapshot (irreversible-protection)

1. Tabla `categoria_it_snapshot_capas` (id PK, `categoria_it_antes`, `relevancia_ia_antes`, `capturado_utc`), misma receta que C2.
2. `INSERT … SELECT` de filas con cat o ia. `ON CONFLICT DO NOTHING`.
3. `migraciones_datos.nombre = 'capas_fase0_snapshot'`.
4. **Validar:** count snapshot = count universo IT; las 54 C1 y 90331 están.

No se toca `clasificacion_contrato`. Reversible: DROP de la tabla snapshot (no se hace).

**Si esto no está, no se sigue.** **Aplicada** (`docs/capas_fase0_snapshot.sql`, marcador `capas_fase0_snapshot`).

### Fase 1 — Expand DDL

CREATE de las 5 tablas nuevas + índices + RLS + grants. Sin backfill. Sin vistas tocadas. Sin trigger de eco.

**Validar:** `contratos` igual (columnas y counts); front idéntico; tablas nuevas COUNT=0; `NOTIFY pgrst`.

Rollback: `DROP TABLE … CASCADE` de las cinco. No hay datos.

**Aplicada** (`docs/capas_fase1_tablas.sql`, marcador `capas_fase1_tablas`).

### Fase 2 — Backfill capa 3

INSERT clasificación (§6). Lectores siguen en `contratos`.

**Aplicada** (`9c7528b`, marcador capas fase 2).

### Fase 3 — Backfill ítems y documentos

**Aplicada** (`151adc5`): `contrato_items` **7370** filas de 3977 contratos (índice `cod_cubso`); `documentos` **3796** filas, **925** con `storage_path`. Columnas viejas siguen. Front no las usa todavía.

### Fase 4 — Dual-write — APLICADA

Orden aplicado:

1. Trigger `trg_clasificacion_echo` (`docs/capas_fase4_eco.sql`, marcador `capas_fase4_eco`).
2. Escritores → `clasificacion_contrato`: `backfill_categoria.py`, `clasificar_gemini.py --aplicar`, `reclasificar_categoria.py`, `ingesta_completa.py` (solo altas, post-upsert).
3. Ingesta **no** manda `categoria_it` / `relevancia_ia` en el upsert.
4. Guard `SEACE_FORZAR_COMPLETA` retirado.

**Validar:** diff clasificacion vs contratos = 0; C1 (54) con `capa='gemini'`.

**Riesgo PostgREST (mitigado):** el upsert de contratos **omite** las claves de inferencia; PostgREST no las toca en UPDATE. Altas nuevas nacen con NULL en esas columnas hasta que el eco las rellena desde capa 3.

### Fase 5 — Lectores SQL a JOIN (sin DROP)

`CREATE OR REPLACE` de `v_contratos_estado`, `v_kpis_*`, `v_kpis_conversion*`, `dashboard_resumen`, `vigentes_urgentes`, `buscar_contratos` leyendo `clasificacion_contrato`. Crear `v_contratos`.

Front puede seguir en `contratos` (eco). Preferible un PR chico: Ruta/Buscador/fallback Dashboard → `v_contratos` (mismos nombres de campo).

**Validar (prod, lectura):**

- `total_postulables` y `por_linea` = foto pre-deploy (Dashboard).
- RPC `buscar_contratos` con un término conocido (p. ej. una entidad) devuelve las mismas `categoria_it`.
- Worker `/analizar` de un id de prueba: prompt sigue viendo categoría.

Rollback: restaurar definiciones viejas de vistas (guardar el SQL actual en el PR). Columnas de `contratos` intactas.

### Fase 6 — Contract: DROP en `contratos`

Solo si fase 5 lleva ≥1 ciclo diario de pipeline + front ya no selecciona `contratos.categoria_it` **o** el eco sigue (entonces hay que migrar front primero).

1. Front + Worker leen `v_contratos` / JOIN.
2. `DROP TRIGGER trg_clasificacion_echo`.
3. `ALTER TABLE contratos DROP COLUMN categoria_it, DROP COLUMN relevancia_ia`.
4. `DROP INDEX idx_contratos_catit` (cae con la columna).
5. `NOTIFY pgrst`.

**Validar:** las mismas queries de fase 5. 54 C1 siguen en `clasificacion_contrato`.

**Irreversible sin snapshot.** Reconstruir columnas desde `clasificacion_contrato` + snapshot fase 0 es el plan B (horas, no un botón).

### Fase 7 — Contract opcional (misma capa 1)

Cuando el front lea `contrato_items` y `documentos`:

- DROP `items_json` (después de que ficha / format.ts no lo usen).
- DROP `pdf_archivo_id`, `pdf_storage_path`, `pdf_hash`, `pdf_nombre` (dejar `tdr_texto` y colas OCR hasta otro diseño).

No es bloqueante. Duplicar 4 000 JSON y ~2 000 PDFs es barato frente a romper la ficha.

`analisis_contrato` se enciende cuando el Worker dual-escriba KV+PG. No bloquea capas 1–3.

---

## 8. Riesgos

| Riesgo | Qué se rompe | Irreversible | Mitigación |
|---|---|---|---|
| Upsert PostgREST NULLA columnas omitidas | Todas las etiquetas → NULL en una corrida de ingesta | Casi: snapshot fase 0 + clasificacion | Staging; `columns=` ; no cortar dual-write hasta verlo |
| DROP `categoria_it` con front viejo | Ruta, Dashboard, Buscador, pills, Worker prompt | El DROP sí; datos no si hay snapshot | Fase 6 solo con `v_contratos` en prod |
| `--forzar-completa` durante fases 0–3 | Pisa C1 **igual que hoy** | Sí sobre `contratos` | Moratoria del flag hasta fase 4 cerrada |
| Vistas `v_kpis_*` mal joineadas | KPIs a 0 o universo 77k | No | Diff de un recorte de `v_kpis_dashboard` antes/después |
| `buscar_contratos` SECURITY DEFINER desactualizado | Chat RRF y Buscador relevancia | No | Reescribir en el mismo PR que las vistas |
| Trigger eco + ingesta aún escribiendo cat | Carrera: último writer gana | No | Orden fase 4: eco **después** de cegar ingesta, o al revés con ventana corta y C1 chequeado |
| FK rígida a CUBSO | Falla `enriquecer_detalle` | No | No hay FK (§2.4) |
| Auto-activar Tipo B ancho | Segunda `impresora` (769 etiquetas) | `UPDATE it_keywords.activa=false` revierte la regla; las filas ya etiquetadas no se deshacen solas | UMBRAL_AUTO=50 + cambios_categoria=0 + MIN_VECES=3. Lo que pasa el techo queda `medida` (§11) |
| Tipo A corto (`nas`/`na`) | Basura por Levenshtein en keywords de 2–3 letras | Reversible con `activa=false` | Tipo A solo si `min(len) >= 6` (Levenshtein) o prefijo ≥5 y ≥60 % de la madre |
| Truncate / DROP de `clasificacion` post-eco-off | Radar vacío | Si ya se dropearon columnas, sí | Nunca truncar sin snapshot |
| RLS sin GRANT | Front 401 en vista nueva | No | Mismo patrón `anon, authenticated` que `contratos` |
| `analisis_contrato` público | Caché de juicio ENERTRONIC legible con anon key | No (ya se sirve por Worker) | Aceptado; no inventar auth |

**Dónde hace falta snapshot**

1. **Fase 0 (obligatorio):** etiquetas actuales.
2. **Antes del DROP fase 6:** dump de `clasificacion_contrato` (pg_dump tabla, o segunda tabla snapshot). Con eso se recrean columnas si el contract sale mal.
3. **No hace falta** snapshot de `items_json` ni PDF: el origen sigue en `contratos` hasta fase 7.
4. **No dump de KV** para análisis: TTL 3 días, no es historia.

`categoria_it_snapshot_c2` no se toca.

---

## 9. Qué no hay que hacer (sobreingeniería)

- **`clasificacion_historial` en v1** (§2.1).
- **Tabla `cotizaciones`** reemplazando `cotizar_tipo_log`.
- **Mover `analizado`/`cotizado`** a “existe análisis”.
- **FK `cod_cubso` → `cubso_catalogo`.**
- **N:M candidata↔contrato** (el array alcanza).
- **Auto-activar Tipo B** por encima de `UMBRAL_AUTO` o con `cambios_categoria > 0` (eso sí es sobreingeniería peligrosa). Tipo A y Tipo B bajo umbral **sí** se activan solas (§11).
- **Pedir un humano** para que el clasificador siga cubriendo el corpus. El admin es opcional.
- **Columnas por campo** de `AnalisisPayload`.
- **Historial de análisis** por `prompt_version` (UPDATE in-place).
- **Una fila `documentos` por página** / mover colas OCR.
- **Clasificar por segmento CUBSO 43/81** (`capa='cubso'` queda reservado, 0 filas).
- **Particionar, CDC, outbox, event sourcing.**
- **RLS por entidad o por usuario.** El SEACE es público; el patrón actual basta.
- **Reescribir `texto_busqueda`** para incluir ítems (otro proyecto).
- **Tocar `chunks_tdr` / embeddings / `buscar_tdr_v2`.**
- **Borrar `items_json` o `pdf_*` en el mismo contract que `categoria_it`.**
- **Editar clasificación desde el browser** (C3 UI es otro diseño; si existe, escribe `capa='humano'` con service_role vía API admin, no con JWT de lectura).
- **Reconstruir `keyword_id` histórico** sobre las 4 213.
- **Correr `--forzar-completa`** para “probar” la migración (moratoria hasta fase 6; guard `SEACE_FORZAR_COMPLETA`).
- **Fase 2** (backfill `clasificacion_contrato`) hasta que se pida.

---

## 10. Orden de aprobación

1. Capas y reglas de escritura (§1).
2. No-historial v1, JSONB de análisis, no-tabla cotizaciones, no-FK CUBSO (§2).
3. DDL + RLS (§4).
4. Vista `v_contratos` + trigger eco como puente de front (§4.6–4.7, §5).
5. Provenance 54 / 90331 / keyword (§6).
6. Fases 0→6 (§7) y moratoria de `--forzar-completa`.
7. Aprendizaje autónomo de vocabulario (§11): umbrales, pistas Gemini, UI admin.

**Hecho (6 sep 2026):** fases 0–4 (dual-write + eco). Pendiente cuando se pida: fases 5–6 (lectores / DROP), no el job de aprendizaje (§11 diseñado, **no** implementado). C4 Gemini semanal ya corre; historial append-on-change (§2) sigue opcional.

---

## 11. Aprendizaje autónomo de vocabulario

**Estado: diseñado, no implementado** (pendiente 6 sep).

**Principio:** el sistema es autosuficiente. No pide aprobación humana para funcionar. El admin puede revisar y editar; es opcional y esporádico.

Hoy Gemini entiende un título truncado (`desarro`) y las keywords no. Esa señal se pierde en el artefacto. El flujo la convierte en regla (si es segura) o en pista de desambiguación (si es ancha).

### 11.1 Flujo

1. Gemini clasifica un contrato que las keywords dejaron en NULL. Copia la señal literal: `verificar_senal()` / `verificar_senal_p2()` ya exigen que el substring exista en descripción, objeto o ítem. Sin match → confianza baja; no entra como candidata.
2. Si esa señal (normalizada: minúsculas, sin tildes, recortada) **no** está como `it_keywords.keyword` activa de tipo `incluye` en esa categoría, se registra en `keyword_candidatas`: `estado='nueva'`, se incrementa `veces_vista`, se agrega el `contrato_id` al array, se actualiza `ultima_vez_utc`. **Nunca** se auto-aprueba en este paso.
3. Job de evaluación (pipeline, no browser; **no existe aún** — diseño). Sobre cada fila `nueva` o `medida` desactualizada:

**Tipo A — variante de una keyword existente.**  
La señal es prefijo o sufijo de una `incluye` activa, o dista ≤2 caracteres (Levenshtein), **en la misma categoría propuesta**. Ejemplos: `desarro` vs `desarrollo`, `hostin` vs `hosting`, `escanner` vs `escaner`.

Guardas (sin ellas, C2 midió basura `nas`→`na` y ` ups `→PICK UP):

- Levenshtein: `min(len(madre), len(señal)) >= 6`.
- Prefijo/sufijo: el más corto tiene ≥5 caracteres **y** ≥60 % de la longitud de la madre (`des` no es variante de `desarrollo`).
- Solo se compara contra `tipo='incluye'` y `activa=true` de **la misma** `categoria_propuesta`. No se cruza de categoría.
- No se auto-crean `excluye`.

→ se inserta en `it_keywords` (misma categoría y `prioridad` que la madre; `limite_palabra` copiado; `tipo='incluye'`). Candidata → `estado='auto_activada'`, `tipo_eval='a'`, `keyword_madre_id`, `keyword_id`. **No** se mide universo A: es la misma palabra mal escrita.

**Tipo B — término nuevo.**  
No se parece a ninguna keyword activa. **Antes** de activar se mide contra el corpus (misma receta que las auditorías de candidatas):

| Métrica | Qué es |
|---|---|
| `universo_a` | Contratos que la señal etiquetaría (match substring, hoy NULL o no). |
| `cambios_categoria` | Ya etiquetados cuya categoría **cambiaría** si la keyword ganara la cascada. |
| `ya_etiquetados` | Ya etiquetados donde aparece la señal. |
| `ratio_predictivo` | `ya_etiquetados / universo_a` (NULL si A=0). |

Regla de activación automática (las tres):

```
universo_a <= UMBRAL_AUTO
AND cambios_categoria == 0
AND veces_vista >= MIN_VECES
AND la misma categoria_propuesta en esas vistas
```

Si se cumple: INSERT `it_keywords` + `estado='auto_activada'`, `tipo_eval='b'`.  
Si `universo_a > UMBRAL_AUTO` **o** `cambios_categoria > 0`: `estado='medida'`. **No** se activa. Sirve de pista para Gemini (§11.3).  
Si aún no llega a `MIN_VECES`: se queda `nueva` (sigue contando).

4. Las candidatas **no** activadas (`nueva` con `veces_vista>=2`, y todas las `medida`) se inyectan en el prompt de desambiguación como vocabulario, no como reglas (§11.3).
5. Toda activación deja rastro en la candidata: `activada_por` (nombre del proceso, p. ej. `evaluar_candidatas.py`), `activada_utc`, `evidencia` jsonb (madre, distancias, A, cambios, MIN_VECES), `keyword_id`. Reversible: `UPDATE it_keywords SET activa=false WHERE id = keyword_id`. No hace falta borrar la fila ni desetiquetar el corpus (el próximo backfill de keywords, cuando exista, respeta `capa IN ('gemini','humano')`).

### 11.2 Umbrales

| Constante | Valor | Por qué |
|---|---|---|
| `UMBRAL_AUTO` | **50** | Las candidatas limpias que ya aceptamos a mano rondaban 2–50 (`equipos de computo` → 45). Por encima empiezan los términos anchos que **no** deben volverse regla: digitalización ~210 (mezcla TEC/locación), trámite documentario ~154. El contraejemplo de C2: **`impresora` sola = 769 de 3240 etiquetas (24 %)**. 50 es ~15× más chico que esa keyword tóxica y coincide con el techo de lo que ya dimos por bueno. |
| `MIN_VECES` | **3** | Mismo listón que C1 (consenso de 3 corridas Gemini). Una sola vista puede ser un contrato raro o una señal recortada de un lote. Tres vistas **con la misma categoría** no son un empate entre categorías: si Gemini dijo Hardware dos veces y Redes una, no se activa (hay que partir la candidata por `(senal, categoria_propuesta)`; el UNIQUE ya es ese par). |

No se sube `UMBRAL_AUTO` “para cubrir digitalización”: ese término tiene que quedarse `medida` y entrar al prompt como pista. Activarlo como keyword reabre el ruido de locación de personal.

Constantes de código (cuando se implemente el job), no de SQL. Cambiarlas no es migración.

### 11.3 Dónde entra en `clasificar_gemini.py` (sin inflar el prompt)

No tocar `SYSTEM_PROMPT_REGLAS` ni `DEF_CATEGORIAS`: el prefijo del system se cachea y ya lista las 13 categorías. Meter ahí 50 candidatas duplica costo en **todas** las llamadas (P1 y P2).

La desambiguación es la **pasada 2** (`comando_proponer` → `user_prompt_p2`, ~L583–595): ciega a entidad/área/CUBSO, solo objeto + descripción + ítem. Ahí Gemini decide categoría sin el sesgo del área. Las pistas de vocabulario **no** son sesgo de entidad; son palabras que el propio modelo ya usó como señal.

Inyección (diseño, no código ahora):

- Función `bloque_pistas_vocabulario(candidatas) -> str` al **final** de `user_prompt_p2`, después de los contratos del lote.
- Tope **20 líneas**, ~800 caracteres. Orden: `medida` primero (ya medidas, A grande), luego `nueva` con `veces_vista >= 2`. `ORDER BY veces_vista DESC`.
- Formato de una línea, no prosa: `desarro → Desarrollo software (pista; no es regla)`.
- No incluir `auto_activada` / `aprobada_admin` (ya son `it_keywords`).
- No incluir `rechazada`.
- P1 **no** lleva el bloque: P1 ya copia la señal; es el lote más caro por contrato (más campos). Las pistas sirven para el desempate, no para descubrir la señal.

Si el bloque está vacío (tablas vacías, fase 1), `user_prompt_p2` queda exactamente como hoy.

### 11.4 Estados

| Estado | Quién lo pone | Qué significa |
|---|---|---|
| `nueva` | Gemini al registrar la señal | Aún no se mide, o no llega a `MIN_VECES`. |
| `medida` | Job eval tipo B | Corpus medido; **no** se activa (A>50 o cambiaría etiquetas). Pista Gemini. |
| `auto_activada` | Job eval tipo A o B bajo umbral | Hay fila en `it_keywords` (`keyword_id`). |
| `aprobada_admin` | Worker admin (§11.5) | Humano promovió una `medida`/`nueva` (p. ej. digitalización). |
| `rechazada` | Worker admin | No se vuelve a proponer ni se inyecta al prompt. |

No hay `pendiente` / `aprobada`: esos nombres del primer borrador se reemplazan. Fase 1 crea la tabla ya con este CHECK.

### 11.5 Vista admin (diseño, no implementación)

Pantalla: sección nueva en `/observabilidad` (ya gated `perfil.rol === 'admin'`) o ruta `/vocabulario` con el mismo guard. No hace falta un producto aparte.

**Lectura (browser, PostgREST):** `it_keywords` y `keyword_candidatas` ya tienen RLS `es_admin()`. SELECT de activas, inactivas, candidatas por estado. Filtros: categoría, estado, `veces_vista`.

**Escritura: no el JWT, no la anon key.** Este repo **no** tiene Supabase Edge Functions. El borde que ya corre con `SUPABASE_SERVICE_KEY` y `requireAdmin()` es `seace-ai-proxy` (`GET /admin/stats`). Ahí va un `POST /admin/keywords` (mismo Bearer del usuario logueado → Auth → `perfiles.rol='admin'` → escribe con service_role). Equivale a la “Edge Function con service-role” pedida; no se añade un runtime Deno.

Contrato del endpoint (una acción por request):

| `accion` | Body | Efecto |
|---|---|---|
| `crear` | `{keyword, categoria, tipo, limite_palabra, nota}` | INSERT `it_keywords` (`origen` implícito admin; `prioridad` = la de esa categoría). |
| `set_activa` | `{id, activa}` | `UPDATE it_keywords SET activa=…`. Reversible. |
| `promover_candidata` | `{candidata_id}` | INSERT keyword desde la señal + `estado='aprobada_admin'` + `keyword_id`. No corre el umbral (el humano ya lo vio). |
| `rechazar_candidata` | `{candidata_id, nota}` | `estado='rechazada'`. |

Respuestas: 401/403 igual que `/admin/stats`; 409 si UNIQUE `(categoria, keyword, tipo)` choca. El browser nunca ve service_role.

No se edita `clasificacion_contrato` desde esta pantalla (eso sería C3, `capa='humano'`).

### 11.6 Qué no se implementa ahora

- El job `evaluar_candidatas.py`.
- El bloque en `user_prompt_p2`.
- `POST /admin/keywords` y la UI.
- ALTER de `it_keywords` (el rastro vive en la candidata; `activa=false` ya existe).
- Fase 2.

---

## 12. `--forzar-completa` (post fase 4)

Desde la fase 4 la ingesta **no** incluye `categoria_it` / `relevancia_ia` en el upsert de `contratos`. La inferencia va a `clasificacion_contrato` con `capa='keyword'` y **no pisa** `gemini`/`humano`. El guard `SEACE_FORZAR_COMPLETA` se retiró.

Sigue siendo una operación cara (re-descarga del corpus). No hace falta para backfill de keywords (`reclasificar_categoria.py` / `backfill_categoria.py`).
)
