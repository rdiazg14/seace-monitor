# Changelog de iteraciones — asesor SEACE

Resumen para retomar **sin chat previo**. Detalle de cómo/por qué: [ARQUITECTURA_TECNICA.md](./ARQUITECTURA_TECNICA.md). Foto de prod iter. 1–11: [ESTADO_CIERRE_2026-08-29.md](./ESTADO_CIERRE_2026-08-29.md) (histórico 1–9: [ESTADO_CIERRE_2026-08-20.md](./ESTADO_CIERRE_2026-08-20.md)). Clasificación IT y cierres: [TRASPASO_MAESTRO_SEACE.md](./TRASPASO_MAESTRO_SEACE.md) §6.

Corte: **6 sep 2026** (Perú). Contratos de calibración: **87880** SEDAPAR, **87502** CENEPRED. Clasificación IT: **90432** (keywords), **90331** (Gemini). C4: **92081/92070/91928/91674**.

Repos: `seace-monitor` · `seace-web` · `seace-ai-proxy` · `seace-pipeline-trigger`.

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
| — | Clasificación IT Fase A | Keyword `implementacion de software` → Desarrollo software. `reclasificar_categoria.py` reaplica keywords sobre ambas columnas NULL (no Gemini, no pisa etiquetas). Tapa 3 vigentes: **28275**, **31625**, **90432** | `ingesta_completa.py` `IT_CATS`; `reclasificar_categoria.py` | `1004b02`, `24a2a3b` | **prod** |
| — | Clasificación IT Fase B | Gemini Flash batch + enum 13 + `ninguna` sobre nulls. Por objeto, no por área. **Manual:** dry-run vigentes 1 ≠ write 3; FP Hardware 90592/90386 revertidos. `temperature: 0` no elimina drift. No en `pipeline.yml` | `clasificar_gemini.py` | `d430272` | **prod** (manual; no automatizar sin Arquitectura C) |
| — | Cierre 3–5 sep | C1 (Arquitectura C fase 1): clasificador con `--proponer`/`--aplicar`/`--consenso`, verificación de señal literal, desempate ciego, ledger de rechazos. 54 contratos escritos por consenso de 3 corridas sobre 1802. Manual; no entra al cron hasta C4. B21: `fecha_publica`, `fecha_ini_cotizacion` y `fecha_fin_cotizacion` estaban 5h atrasadas (SEACE entrega hora de pared de Lima; `parsear_fecha` pegaba `+00:00`). Backfill de 77485 filas. pct horario hábil 48.9% → 78.1%. B12 re-medido con fechas corregidas: 17.5% inalcanzable (78/445), no el 27.4% del proxy «ventana &lt;24h». Detección temprana: workflow de 2m16s cada 2h (ingesta + detalle). B20 cuantificado: `schedule` de GHA con 4h04m de atraso medio; el cron del Worker llega con ~26s. C2 en 4 fases: keywords a tabla `it_keywords`, ingesta consumiéndola, 6 includes + 15 exclusiones, backfill del corpus (697 altas, 53 cambios, 222 desetiquetadas). Hardware 1475 → 1231. Scripts nuevos: `run_sql.py`, `validar_keywords_tabla.py`, `backfill_categoria.py` | Monitor `clasificar_gemini.py`, `ingesta_completa.py`, `docs/b21_fix_timezone.sql`, `docs/c2_keywords_*.sql`, `docs/c2_fase4_snapshot.sql`, `.github/workflows/deteccion_temprana.yml`, `scripts/run_sql.py`, `scripts/validar_keywords_tabla.py`, `scripts/backfill_categoria.py` | `6de09f9`, `dd084c2`, `ba27371`, `cbca110`, `e4f238a`, `666f108`, `d10051d`, `17ea24e`, `e677c1f`, `c7d5b7c` | **prod** (C1 base; C2 en tabla + backfill; C4 llegó el 6 sep) |
| — | Cierre 6 sep | **C4:** keywords diario en pipeline (`reclasificar` 16481→1 en 68s) + Gemini semanal ×3 (universo 264, 4 unánimes, ~USD 0,19; ids 92081/92070/91928/91674); cupo `clasificacion_cuota.json`. **Score enriquecido:** `analisis_contrato` manda; techos califica=no→35 y margen&lt;1000→55; 91688=93, 92065=92, 91696=35; select 37 KiB. **Capas fase 3:** contrato_items 7370 / documentos 3796 (925 con storage). **CUBSO** 2026-07-02: huérfanos 1772→30, BD 290115. **Seguridad:** RLS snapshots; Worker `requireSesion`; SEGURIDAD.md; trigger versionado. **Data lake:** 925 PDFs / 1,04 GB | Monitor C4/workflows/capas/CUBSO/seguridad; web `rutaDia.ts`; worker JWT; trigger repo | monitor `916d865`/`151adc5`/`c3002bd`/`2f08dbc`; web `1ffe8b0`; worker `6e62b74`; trigger `060a215` | **prod** |

## Qué no cubren estas iteraciones

Siguen fuera (backlog real; ver TRASPASO §6 cierre 6 sep):

- Home de la app = Ruta del día (`/` sigue siendo Dashboard).
- Tarea #4 chunking (overlap / tamaño vs baseline 63%).
- Fase 7: dropear `embedding(768)` + ivfflat.
- Brief diario automático (#12).
- Reescribir query del chat RAG con historial.
- Retry de `/analizar` / no cobrar cupo si Gemini falla (el 502 amable **no** tocó el cupo).
- Caché semántica de `/cotizar` (solo exacta + `esCacheable`).
- Chat que responda KPIs de la capa semántica.
- C3 (cola de revisión admin: 13 items + 4 observaciones).
- Capas fases 4–6 (dual-write / DROP); moratoria `--forzar-completa`.
- Aprendizaje de vocabulario (ARQUITECTURA_DATOS §11, diseño sin código).
- Vista admin de keywords.
- Data lake histórico (946 vigentes &gt;90 días).
- PAT fine-grained del trigger; B20 (atraso GHA); 30 códigos CUBSO huérfanos.
