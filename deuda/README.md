# Herramientas de diagnóstico, evaluación y archivo técnico

Esta carpeta reúne scripts que no forman parte del pipeline ordinario ni de la
operación manual vigente. Algunos leen producción; otros escriben datos,
reemplazan chunks o consumen APIs. Nada se elimina: el contenido se conserva
para recuperación y evidencia.

Antes de ejecutar un archivo:

1. Buscar consumidores en código y workflows.
2. Leer argumentos, variables y operaciones de persistencia.
3. Identificar su clasificación en la tabla de abajo.
4. Preparar respaldo, límite y recuperación cuando escriba.
5. Registrar la evidencia en la tarea correspondiente.

No ejecutar scripts por su nombre ni importarlos como smoke test. Los que
viven en `scripts/` o la raíz sí forman parte del producto: allí quedan los
comandos productivos referenciados por workflows (`analizar_postulables`,
`verificar_capas`, `evaluar_candidatas`) y las operaciones manuales vigentes
(`run_sql`, `cargar_cubso`).

Los scripts archivados cargan el `.env` de la raíz mediante `_bootstrap.py` o
su propia resolución `Path(__file__).parent.parent`, que sigue apuntando a la
raíz del repositorio.

## Diagnóstico reutilizable

Herramientas de solo lectura o verificación que pueden volver a necesitarse:

- `auditoria_rag.py` — queries semánticas Gemini + `buscar_tdr_v2` vs FTS.
- `auditoria_supabase.py` — auditoría de datos en Supabase.
- `diag_contrato_rag.py` — diagnóstico RAG de un contrato puntual.
- `medir_volumen.py` — volumen del buscador SEACE vía API interna.
- `probar_buscar_tdr.py` — búsqueda semántica contra `buscar_tdr_v2`.
- `probar_pdf_rag.py` — prueba PDF → RAG v2.
- `smoke_front_v_contratos.py` — smoke de `v_contratos` y vistas KPI.
- `smoke_worker_v_contratos.py` — smoke de ficha vía `v_contratos` (camino Worker).
- `validar_diff_capas.py` — sync `clasificacion_contrato`↔`contratos` y escritores fase 4.
- `validar_fase4.py` — validaciones fase 4 post-pipeline/ingesta.
- `validar_keywords_tabla.py` — `it_keywords` vs `clasificar_categoria_it` (solo lectura).
- `validar_pdf_rag.py` — queries de detalle TDR sobre el RAG v2.
- `verificar_etapas.py` — cobertura de `etapas_json` en el universo de Ruta del día.

## Evaluación

Mediciones A/B y barridos offline sin escritura productiva:

- `eval_chunking.py` — A/B de estrategias de chunking del TDR.
- `eval_rerank.py` — post-rerank (bge-reranker-base) vs RRF.
- `eval_retrieval.py` — precisión/recall del retrieval v2.
- `eval_sweep.py` — sweep de threshold/RRF_K/top_k del reranker.

## Migración o backfill de una ejecución

Ya se ejecutaron contra producción; se conservan para evidencia y referencia:

- `backfill_categoria.py` — C2 fase 4: `categoria_it` con cascada de `it_keywords`.
- `backfill_embed_text.py` — `chunk_embed_text` + reset `embedding_v2` (PDF con membrete).
- `backfill_etapas.py` — `etapas_json` del universo Vigente/En Evaluación.
- `capas_fase2_keyword_id.py` — `keyword_id` en `clasificacion_contrato` (fase 2).
- `capas_fase4_sync_huecos.py` — etiquetas post-fase-2 que solo estaban en `contratos`.
- `capas_fase5_baseline.py` / `capas_fase5_verify.py` — baseline y comparación de KPIs fase 5.
- `limpiar_fantasmas.py` — contratos fantasma del upsert de `refrescar_estados_bloque`.
- `limpiar_fecha_corrupta.py` — fechas de cotización con año absurdo → NULL.
- `migrar_chunk_300_60.py` — re-chunking 500/0 → 300/60 en postulables TI/IA.
- `reparar_analizado_flag.py` — `analizado=false` en flags sin payload (falso positivo del funnel).
- `subir_pdf_storage.py` — backfill de PDFs al bucket `tdr` (el pipeline ya sube al capturar).

## Evidencia histórica y pruebas puntuales

- `descubrir_api_detalle.py` — descubrimiento de la API de detalle SEACE (fase 1.1).
- `descubrir_endpoint_pdf.py` — descubrimiento del endpoint de descarga del requerimiento.
- `probar_eco_fase4.py` — prueba puntual del trigger de eco (capas fase 4).
- `probar_fallback_actions.py` — desetiquetar/restaurar una keyword por el camino Actions.
- `probar_supa_fallback_fase4.py` — `escribir_keyword` vía supabase sin `DATABASE_URL`.
- `verificar_schema_rag.py` — verificación del schema RAG creado (fase 1.3).

Las referencias cruzadas internas (p. ej. `capas_fase2_keyword_id` importa de
`backfill_categoria`) resuelven dentro de esta misma carpeta.
