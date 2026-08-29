# Estado de cierre — 29 ago 2026

Foto para retomar **sin chat previo**. Cómo/por qué: [ARQUITECTURA_TECNICA.md](./ARQUITECTURA_TECNICA.md). Historia: [CHANGELOG_ITERACIONES.md](./CHANGELOG_ITERACIONES.md). Punto de entrada: [TRASPASO_MAESTRO_SEACE.md](./TRASPASO_MAESTRO_SEACE.md).

El corte del 20 ago ([ESTADO_CIERRE_2026-08-20.md](./ESTADO_CIERRE_2026-08-20.md)) sigue siendo la foto de iteraciones **1–9 + fixes**. Este archivo cubre lo que se sumó **después**: self-routing #11, funnel KV→PG y Dashboard de conversión 30d.

Corte: **29 ago 2026** (Perú). Iteraciones **1–11** en producción. Calibración: **87880** SEDAPAR, **87502** CENEPRED.

---

## Qué está en producción

| Pieza | URL | HEAD / versión viva |
|---|---|---|
| Front (GitHub Pages) | https://seace.rdiaz-lab.xyz | `6f1a2f9` (Dashboard conversión 30d) |
| Worker | https://seace-ai-proxy.rdiazg14.workers.dev | git `fdcc7fd` · CF **`fcefa7a0-e623-432b-8479-71b576927cda`** |
| Pipeline | cron 09:00 Perú (`0 14 * * *`) | `a060d2a` + `reconciliar_funnel.py` (best-effort tras embed) |
| Supabase | `wusywwhcyqngnpvpzxyr` | Columnas funnel + vistas `v_kpis_conversion` / `_rubro` **aplicadas** (GRANT anon) |

Flujos vivos (además de #9/#10/#11/RAG/login del 20 ago):

- **#11 self-routing** — `POST /cotizar` ya **no** clasifica con Flash aparte. Un generate; el modelo elige `tipo_respuesta`. MISS = **Δ1**. HIT = 0 Gemini.
- **Funnel** — marcas permanentes `funnel:analizado:{id}` / `funnel:cotizado:{id}` (sin TTL). `GET /funnel-pendientes` (Bearer `FUNNEL_TOKEN`). Cron vuelca a `contratos.analizado/cotizado/fecha_*`.
- **Dashboard conversión** — cobertura (radar IT 30d) vs ejecución (postulables). `fmtTasa(null)` → "—", nunca 0%.

---

## Iteraciones 10–11 (encima de 1–9)

| # | Capacidad | Git | Deploy |
|---|---|---|---|
| 10 | Self-routing `/cotizar` (sin clasificador Flash). Cleanup de código muerto (`clasificarPorReglas`, `clasificarIntentFlash`, `aplicarFormatoSugerido`). Vivos a propósito: `parseIntent`, `IntentCotizar`, `IntentSource` (HIT caché). | Worker `91e8484` + `fdcc7fd` | CF `fcefa7a0-e623-432b-8479-71b576927cda` |
| 11 | Funnel KV permanente + `GET /funnel-pendientes` + reconciliación PG + vistas + Dashboard 30d | Worker `d1832cc`; monitor `cc75cf3` + `a060d2a`; web `6f1a2f9` | Worker (incluido en el HEAD); SQL aplicado a mano; Pages del `6f1a2f9` |

Detalles de funnel (no romper):

- Marca **independiente** de `esCacheable`. HIT y MISS cuentan. 409 `sin_analisis` y 502 **no** marcan cotizado. `/analizar` 502 **no** marca analizado; HIT de análisis **sí**.
- Idempotente: GET KV antes de PUT; no pisa fecha. Reconciliación copia el ISO del KV, **no** `now()`.
- Prefijo `funnel:` distinto de `analyze:` / `chat:` / cupos. Mismo namespace `CHAT_LIMITS`.
- `FALSE` en `contratos` = nunca marcado **desde que existe la columna**, no “nunca se analizó en la historia”.

---

## Cupos `/cotizar` (cambio vs 20 ago)

| Caso | Gemini | Cupo global `cotizar:{day}` |
|---|---|---|
| HIT caché chat | 0 (solo RPM) | no |
| MISS | **1** generate (self-routing) | **Δ1** |
| 409 `sin_analisis` | 0 | no |

Ya no hay Δ2 de clasificador. Los contadores `chat_rules:` / `chat_flash_clasif:` **dejaron de escribirse**; puede quedar basura con TTL diario. Nuevo: `cotizar_tipo:{tipo}:{day}`.

---

## Backlog vivo (igual que el 20 ago; nada de esto bloquea)

Home = Ruta · #4 chunking vs 63% · Fase 7 drop 768 · #12 brief diario · aligerar `v_kpis_dashboard` · caché semántica `/cotizar` · no cobrar ANALYZE si 502 · chat que lea KPIs · reranker v2-m3 · `--gc`.

---

## Git en este corte

| Repo | `origin/main` | Working tree |
|---|---|---|
| seace-web | `6f1a2f9` | limpio |
| seace-ai-proxy | `fdcc7fd` | limpio |
| seace-monitor | `a060d2a` | ver traspaso: `docs/vista_kpis_conversion.sql` + este paquete de docs |

Snapshot: **29 ago 2026**.
