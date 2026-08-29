# Changelog de iteraciones — asesor SEACE

Resumen para retomar **sin chat previo**. Detalle de cómo/por qué: [ARQUITECTURA_TECNICA.md](./ARQUITECTURA_TECNICA.md). Foto de prod: [ESTADO_CIERRE_2026-08-29.md](./ESTADO_CIERRE_2026-08-29.md) (histórico 1–9: [ESTADO_CIERRE_2026-08-20.md](./ESTADO_CIERRE_2026-08-20.md)).

Corte: **29 ago 2026** (Perú). Contratos de calibración: **87880** SEDAPAR, **87502** CENEPRED.

Repos: `seace-monitor` · `seace-web` · `seace-ai-proxy`.

| # | Título | Qué resolvió | Archivos | Commit(s) | Estado |
|---|---|---|---|---|---|
| 1 | Cerebro análisis (2º orden) | El JSON de `/analizar` deja de ser 5 bloques planos: timeline, ratio de alcance, componentes, contradicciones TDR, N alternativas | Worker `src/analizar.ts` | `75e84af`, `9573c5d` | **prod** |
| 2 | Frontend estructural dinámico | `/analisis/:id` pinta infografía de ratio, N alternativas, economía por componente, contradicciones | Web `AnalisisV2.tsx`, `AnalisisContrato.tsx` | `4a6742f` | **prod** |
| 2.5 | Coherencia económica + consorcio | `economia` del resumen = vía `recomendada`; `admite_consorcio` null si el TDR no lo dice; `optimizacion` = tácticas de ejecución, no vías | Worker `analizar.ts` (`alinearEconomiaConAlternativa`); web badge consorcio | Worker `0b58703`; web `f1263f9` | **prod** |
| 3 | Timeline fishbone | Eje visual con salto de plazo (`momento_dia`); thumbnail + fullscreen | Web `TimelineFishbone.tsx` | `8424cad` | **prod** |
| 4 | Chat routing texto/tabla/gráfica | Clasificador `{nivel, formato, necesita_internet}` + schema condicional; si Gemini omite tabla/gráfica, post-proceso las arma del análisis congelado | Worker `cotizar.ts`, `escenario.ts`; web `ChatTable.tsx`, `ChatChart.tsx` | Worker `28eaf4b` (+ `9f16f99`, `9884ae2`, `103e580`); web `e35de62` | **prod** |
| 5 | Panel lateral atado al contrato | Chat #11 en la misma página del análisis; `key={contratoId}`; persistencia `localStorage chat_escenarios_{id}` | Web `AnalisisContrato.tsx` (`ChatEscenarios`) | `534c1de` | **prod** |
| 6 | Pulido + contratos diversos | `momento_dia` siempre estimado; chips 3 niveles; gráficas tema claro/oscuro; bugs legacy | Worker `completarMomentoDia`; web CSS `--chart-1…6` | Worker `899372a`; web `301a62b` | **prod** |
| 7 | UX conversacional | Panel abierto default desktop ≥1024 px; SSE de **presentación** del campo `escenario`; indicador Analizando→Analizado; sin andamiaje vacío; prosa limpia en nivel 1 | Worker `cotizar.ts`; web `AnalisisContrato.tsx` | Worker `437c265`; web `9426dc1` | **prod** |
| — | Postulabilidad única (#9) | `esPostulable()` = vigente + ventana Lima abierta (o sin fecha). Ranking default solo postulables; chip «En evaluación / cerrados». Brief y KPIs ya usaban esa regla | Web `rutaDia.ts`, `RutaDia.tsx` | `cffcc2b` | **prod** |
| 8 | Clasificador híbrido + caché exacto | Pre-filtro de reglas (confianza alta → 1 Flash, no 2). Caché `chat:{id}:{pdf_hash}:{sha256(query)}` solo si `esCacheable`. Headers `X-Cotizar-Cache` / `X-Cotizar-Intent`. | Worker `cotizar.ts`, `index.ts` (CORS expose) | `ecc186a` | **prod** la caché exacta; el clasificador se **retiró** en #10 |
| 9 | Capa semántica + Dashboard | Vistas SQL `v_contratos_estado`, `v_kpis_dashboard`, `v_kpis_negocio` + `fn_rubro_energetic`. Dashboard lee esas vistas (fallback TS si fallan). Leak de fechas vencidas en filtros «hoy/semana» corregido. KPIs de negocio sobre postulables | Monitor `capa_semantica.sql`; web `capaSemantica.ts`, `Dashboard.tsx` | Monitor `21921ef`; web `b366060` | **prod** (SQL aplicado; si `v_kpis_dashboard` timeout, el front usa fallback TS) |
| — | Buscador un solo badge | `CatItIaPill`: categoría IT **o** IA, nunca ambos. Alineado con Ruta/análisis | Web `Pills.tsx`, `ContratoCard.tsx` | `6ba2eeb` | **prod** |
| — | 502 amable `/analizar` | Worker 502 con cuerpo `{ error: 'analisis_fallido', mensaje, reintentar, detalle_tecnico }`. Front: banner + Reintentar (vuelve a `POST /analizar`). **No** cambia el orden del cupo ANALYZE | Worker `analizar.ts`; web `AnalisisContrato.tsx` | Worker `c8113ae`; web `c0beff4` | **prod** |
| 10 | Self-routing `/cotizar` | Un generate elige `tipo_respuesta`. Se elimina el clasificador Flash (Δ2→Δ1). `completarEstructuras` respeta el tipo que pidió el modelo (no recorta visuales). Cleanup: se borran `clasificarPorReglas`, `clasificarIntentFlash`, `heuristicIntent`, `aplicarFormatoSugerido`. Vivos: `parseIntent`, `IntentCotizar`, `IntentSource` (HIT caché) | Worker `cotizar.ts`, `escenario.ts` | `91e8484`, `fdcc7fd` | **prod** · CF `fcefa7a0-e623-432b-8479-71b576927cda` |
| 11 | Funnel KV → PG + conversión 30d | Marcas permanentes `funnel:analizado/cotizado:{id}` (HIT y MISS; independiente de `esCacheable`; 409/502 no marcan). `GET /funnel-pendientes` con `FUNNEL_TOKEN`. Cron `reconciliar_funnel.py` copia ISO del KV. Columnas + vistas `v_kpis_conversion` (cobertura vs ejecución). Dashboard: `fmtTasa(null)` → "—" | Worker `funnel.ts`; monitor `reconciliar_funnel.py`, `docs/migracion_funnel_conversion.sql`, `docs/vista_kpis_conversion.sql`; web `capaSemantica.ts`, `Dashboard.tsx` | Worker `d1832cc`; monitor `cc75cf3`, `a060d2a`; web `6f1a2f9` | **prod** (SQL aplicado a mano; GRANT anon) |

## Qué no cubren estas iteraciones

Siguen fuera (backlog real; ver estado de cierre 29 ago):

- Home de la app = Ruta del día (`/` sigue siendo Dashboard).
- Tarea #4 chunking (overlap / tamaño vs baseline 63%).
- Fase 7: dropear `embedding(768)` + ivfflat.
- Brief diario automático (#12).
- Reescribir query del chat RAG con historial.
- Retry de `/analizar` / no cobrar cupo si Gemini falla (el 502 amable **no** tocó el cupo).
- Caché semántica de `/cotizar` (solo exacta + `esCacheable`).
- Chat que responda KPIs de la capa semántica.
