# Incidente: análisis de contrato generado sin TDR real

**Fecha del incidente:** 2026-09-11 01:54 UTC (2026-09-10 20:54 Lima)
**Detectado:** 2026-09-10, durante el diagnóstico del contrato 93277 (anexo `.rar`)
**Arreglado por:** commit `39c32a2` — "Fix: elegir_pdf descartaba PDFs reales por el mime de SEACE"
**Fila eliminada:** 2026-09-10, `analisis_contrato` contrato_id = 93277

---

## 1. Qué pasó

El contrato **93277** (`CM-115-2026-CM-UE008`, Unidad Ejecutora 008 Proyectos
Especiales, Ministerio de Cultura) se publicó con un único anexo:
`Requerimiento.rar` (mime `application/x-compressed`).

`elegir_pdf` comparaba `descripcionMime == "application/pdf"` exacto. Como el
`.rar` no pasaba el filtro, el pipeline marcó `req_url='sin_pdf'`,
`pdf_descargado=true` y `tdr_texto=NULL`. **El TDR real
(`Terminos de referencia.pdf`, 299 KB) quedó dentro del `.rar`, sin leer.**

Hasta ahí el comportamiento era el esperado (no había PDF suelto que bajar).
El problema es lo que pasó **después**, en la tarea de backfill
`scripts/analizar_postulables.py`.

## 2. Por qué se generó el análisis

El gate de la cola de análisis era:

```python
def _tiene_texto(tdr_len: int, n_chunks: int) -> bool:
    return tdr_len >= 200 or n_chunks > 0
```

El contrato tenía `tdr_texto` vacío, pero **3 chunks de `fuente='api'`** que el
chunker base había creado a partir de metadatos de la ficha:

| chunk_index | tipo | contenido |
|---|---|---|
| 0 | Descripción general | entidad + objeto + nro (`SERVICIO ESPECIALIZADO EN MONITOREO Y SOPORTE TÉCNICO…`) |
| 1 | Ítem técnico 1 | CUBSO `8010160600331848` + cantidad + lugar |
| 2 | Metadata | entidad, área usuaria, objeto, estado, número |

Ninguno contiene un solo carácter del TDR. Total: **1.253 caracteres** de
metadatos.

Con `n_chunks > 0` el gate daba `True`, así que `/analizar` lo tomó como
candidato válido y produjo un análisis completo (encaje, economía, timeline,
condiciones, veredicto, riesgos contractuales) **a partir de 1.253 chars de
ficha** — sin haber abierto el documento que define el alcance real.

## 3. Qué decía el veredicto (lo engañoso)

El payload resultante declaraba desconocer cosas que **sí constaban en el PDF
sin abrir**, y presentaba como "estimaciones" datos que eran inventados:

| Campo | Valor generado | Realidad |
|---|---|---|
| `condiciones.plazo` | "No consta explícitamente en el extracto" | El TDR real sí define plazos |
| `condiciones.pago` | "No consta en el extracto (generalmente contra entrega de informes…)" | El TDR real sí define forma de pago |
| `economia.lo_que_no_sabe` | "Duración exacta del servicio" | Consta en el TDR |
| `economia.lo_que_no_sabe` | "Cantidad de entregables e informes de seguimiento" | Consta en el TDR |
| `economia.lo_que_no_sabe` | "Perfil profesional exacto y años de experiencia requeridos" | Consta en el TDR |
| `timeline.duracion_total_texto` | "Estimado 60-90 días calendario" | Inferido de la nada |
| `viabilidad.ratio_alcance` | `techo_contrato: 42800`, rango S/ 18,000–32,000 | Inventado sobre un texto que no lo dice |
| `encaje.perfil_pedido` | "Especialista en gestión de inversiones públicas / contratos" | Inferido del objeto, no del TDR |

Bloque de veredicto tal como quedó almacenado:

```json
{
  "codigo": "evaluar",
  "aviso_humano": "El monto y la decisión final de cotizar quedan a criterio de ENERTRONIC.",
  "razonamiento": "Servicio no tecnológico enfocado en gestión pública e inversiones (Invierte.pe). Solo es viable si se dispone de un consultor especializado asociado para tercerizar la ejecución."
}
```

El daño real: un analista leyendo ese veredicto creería que el sistema leyó el
TDR y concluyó que la información no estaba. La verdad es que el TDR nunca se
abrió. La decisión de negocio se habría tomado sobre una premisa falsa.

### Por qué la UI no lo mostraba

`resolverAnalisisParaContrato` cruza `analisis_contrato` con `contratos` por
`pdf_hash`. El contrato tiene `pdf_hash=NULL` → la UI busca `'na'`. La fila de
análisis tenía `pdf_hash='na'`, así que el cruce coincidía, pero la ficha se
arma desde `v_contratos`, que expone `analizado=false` (columna cruda). De ahí
la contradicción observada: análisis existía en la tabla, la ficha decía "sin
análisis".

## 4. Cómo se arregló el gate

El gate ahora exige TDR real y **distingue el origen de los chunks**:

```python
def _tiene_texto(tdr_len: int, n_chunks_pdf: int) -> bool:
    """Solo el TDR real habilita el analisis.

    Los chunks fuente='api' son metadatos de la ficha (descripcion + item +
    entidad). Antes bastaba n_chunks > 0, asi que un contrato sin TDR se
    analizaba con ~1.2K chars de ficha y el veredicto declaraba "no consta"
    sobre lo que si constaba en el PDF sin abrir.
    """
    return tdr_len >= 200 or n_chunks_pdf > 0
```

Se agregó `n_chunks_pdf` (count de `chunks_tdr` con `fuente='pdf'`) en las dos
rutas del script: la consulta SQL (`DATABASE_URL`) y la ruta PostgREST (GitHub
Actions). Los chunks `fuente='api'` ya **no habilitan** análisis.

Comportamiento verificado:

```
93277 real     : tdr_len=0   chunks_pdf=0 -> False   (antes: True con 3 chunks api)
con chunks pdf : tdr_len=0   chunks_pdf=2 -> True
tdr_texto 250  : tdr_len=250 chunks_pdf=0 -> True
tdr_texto 199  : tdr_len=199 chunks_pdf=0 -> False
```

## 5. Decisión sobre la fila: borrar

Se evaluó marcar (`tdr_fuente='api_degradado'`) vs borrar. **Se eligió borrar**
porque agregar un valor al enum de `tdr_fuente` obliga a tocar la UI y el
contrato del campo por un caso único. La evidencia del bug queda en el commit
`39c32a2` y en este documento, no en la tabla de producción.

## 6. Alcance

**Un solo caso en todo el corpus.** Consulta sobre `analisis_contrato`: 62 filas
con `tdr_fuente='tdr_texto'`, 3 con `tdr_fuente='chunks'`, y de esas 3 **solo 1
era degradada** (93277) — las otras dos tenían chunks `pdf` o texto real. El
backfill corrió una sola vez y no se repitió, así que la contaminación no se
propagó.

## 7. Payload completo eliminado (registro histórico)

Se conserva aquí el payload íntegro, tal como estaba en producción antes del
`DELETE`.

### 7.1 Fila de `analisis_contrato`

| columna | valor |
|---|---|
| `contrato_id` | 93277 |
| `pdf_hash` | `na` |
| `prompt_version` | `1` |
| `modelo` | `gemini-3.7-flash` |
| `tdr_fuente` | `chunks` |
| `tdr_chars` | 1253 |
| `creado_utc` | 2026-09-11 01:54:19.960250+00:00 |

### 7.2 Chunks que habilitaron el análisis (`chunks_tdr`, `fuente='api'`)

**chunk_index 0 — Descripción general**

```
[UNIDAD EJECUTORA 008:PROYECTOS ESPECIALES DEL PLIEGO 003 - MINISTERIO DE CULTURA | SERVICIO ESPECIALIZADO EN MONITOREO Y SOPORTE TÉCNICO EN LA GESTIÓN DE INVERSION | CM-115-2026-CM-UE008]
SERVICIO ESPECIALIZADO EN MONITOREO Y SOPORTE TÉCNICO EN LA GESTIÓN DE INVERSIONES Y CONTRATOS EN EL MARCO DEL PROGRAMA CON CUI N°2505321
```

**chunk_index 1 — Ítem técnico 1**

```
[...]
CUBSO: 8010160600331848 - SERVICIO DE SEGUIMIENTO Y MONITOREO DE PROYECTO DE INVERSION PUBLICA. Cantidad: 1 SERVICIO. Lugar: LIMA/LIMA/SAN BORJA. Especificaciones: SERVICIO ESPECIALIZADO EN MONITOREO Y SOPORTE TÉCNICO EN LA GESTIÓN DE INVERSIONES Y CONTRATOS EN EL MARCO DEL PROGRAMA CON CUI N°2505321
```

**chunk_index 2 — Metadata**

```
[...]
Entidad: UNIDAD EJECUTORA 008:PROYECTOS ESPECIALES DEL PLIEGO 003 - MINISTERIO DE CULTURA. Área usuaria: Oficina de Inversiones. Objeto: Servicio. Estado: Vigente. Número: 115.
```

### 7.3 Payload JSON completo

```json
{
  "nro": "CM-115-2026-CM-UE008",
  "url": "https://prod6.seace.gob.pe/buscador-publico/contrataciones/93277",
  "estado": "Vigente",
  "entidad": "UNIDAD EJECUTORA 008:PROYECTOS ESPECIALES DEL PLIEGO 003 - MINISTERIO DE CULTURA",
  "urgente": true,
  "analisis": {
    "encaje": {
      "razon": "El CUBSO y la descripción corresponden a monitoreo y seguimiento de proyectos de inversión pública (gestión administrativa/técnica de proyectos), alejado del núcleo de TI/software/cloud de ENERTRONIC.",
      "rubro": "oportunista",
      "califica": "insuficiente_info",
      "perfil_pedido": "Especialista en gestión de inversiones públicas / contratos (Invierte.pe / Ley de Contrataciones)"
    },
    "resumen": "Servicio especializado en monitoreo y soporte técnico en la gestión de inversiones y contratos para el Programa CUI N° 2505321 de la UE 008 del Ministerio de Cultura.",
    "economia": {
      "supuestos": [
        "Plazo estimado de 60 a 90 días calendario",
        "Contratación de un consultor especialista en Invierte.pe / contrataciones del Estado",
        "Dedicación parcial o completa en sede San Borja"
      ],
      "margen_soles": 10000,
      "pistas_valor": "Servicio de consultoría/soporte técnico a nivel de órdenes de servicio (≤ 8 UIT / S/ 42,800). Estimado para 2-3 meses de especialista.",
      "lo_que_no_sabe": [
        "Perfil profesional exacto y años de experiencia requeridos",
        "Cantidad de entregables e informes de seguimiento",
        "Duración exacta del servicio"
      ],
      "costo_estimado_soles": 20000,
      "valor_estimado_soles": 30000
    },
    "timeline": {
      "hitos": [
        {
          "tipo": "inicio",
          "orden": 1,
          "nombre": "Inicio de actividades y plan de trabajo",
          "es_critico": false,
          "pago_texto": null,
          "tiene_pago": false,
          "momento_dia": 5,
          "nota_critica": null,
          "pago_momento": null,
          "momento_texto": "Día 5"
        },
        {
          "tipo": "entregable",
          "orden": 2,
          "nombre": "Entregable 1: Primer informe de monitoreo",
          "es_critico": true,
          "pago_texto": "Pago armada 1",
          "tiene_pago": true,
          "momento_dia": 30,
          "nota_critica": "Validación de conformidades por la Oficina de Inversiones",
          "pago_momento": "Contra conformidad",
          "momento_texto": "Día 30"
        },
        {
          "tipo": "entregable",
          "orden": 3,
          "nombre": "Entregable Final: Informe de cierre de gestión de contratos",
          "es_critico": false,
          "pago_texto": "Pago final",
          "tiene_pago": true,
          "momento_dia": 60,
          "nota_critica": null,
          "pago_momento": "Contra conformidad final",
          "momento_texto": "Día 60"
        }
      ],
      "duracion_total_texto": "Estimado 60-90 días calendario"
    },
    "veredicto": {
      "codigo": "evaluar",
      "aviso_humano": "El monto y la decisión final de cotizar quedan a criterio de ENERTRONIC.",
      "razonamiento": "Servicio no tecnológico enfocado en gestión pública e inversiones (Invierte.pe). Solo es viable si se dispone de un consultor especializado asociado para tercerizar la ejecución."
    },
    "viabilidad": {
      "ratio_alcance": {
        "lectura": "El alcance de consultoría individual se ajusta cómodamente al presupuesto menor a 8 UIT.",
        "ratio_texto": "0.7×",
        "techo_contrato": 42800,
        "valor_mercado_max": 32000,
        "valor_mercado_min": 18000
      },
      "contradicciones_tdr": [],
      "cotizacion_por_componente": [
        {
          "nota": "Rango de honorarios para consultor senior en proyectos Invierte.pe por 2 a 3 meses.",
          "componente": "Consultoría y monitoreo de inversiones públicas",
          "mercado_max": 32000,
          "mercado_min": 18000
        }
      ]
    },
    "condiciones": {
      "pago": "No consta en el extracto (generalmente contra entrega de informes mensuales/entregables)",
      "plazo": "No consta explícitamente en el extracto",
      "armadas": null,
      "modalidad": "presencial",
      "tono_pago": "warn",
      "tono_plazo": "warn",
      "penalidades": "Según Ley de Contrataciones y directivas internas de la entidad",
      "tono_modalidad": "warn",
      "tono_penalidad": "warn",
      "modalidad_detalle": "Lugar de prestación: Lima / Lima / San Borja (Sede UE 008 / MinCul)"
    },
    "alternativas": [
      {
        "titulo": "Postulación directa con consultor asociado",
        "economia": {
          "nota": "Estimación basada en honorarios de especialista de gestión pública por 2-3 meses.",
          "costo": 20000,
          "valor": 30000,
          "margen": 10000
        },
        "etiqueta": "A",
        "viabilidad": "viable_condicionada",
        "explicacion": "ENERTRONIC presenta la propuesta subcontratando a un especialista en gestión de inversiones públicas que cumpla con los TDR.",
        "recomendada": true,
        "riesgo_clave": "No cumplir el perfil específico de experiencia en inversiones públicas si el postor debe acreditarlo como empresa.",
        "veredicto_corto": "Viable si se cuenta con el perfil"
      }
    ],
    "optimizacion": [
      "Asignar un consultor externo por entregables para reducir costo fijo operativo",
      "Asegurar plantillas estandarizadas de monitoreo de inversiones para reducir horas de elaboración de informes"
    ],
    "chips_sugeridos": [
      "¿Dónde se ejecutará el servicio?",
      "Requisitos del especialista",
      "Distribución de costos estimados"
    ],
    "componentes_servicio": [
      {
        "nombre": "Monitoreo y soporte técnico en gestión de inversiones (CUI N° 2505321)",
        "temario": [
          "Seguimiento a proyectos de inversión pública",
          "Control y monitoreo de contratos de inversión",
          "Elaboración de reportes de avance y alertas técnicas"
        ],
        "horas_min": null,
        "modalidad": "Presencial / Mixto en San Borja",
        "sesiones_min": null,
        "participantes_max": null
      }
    ],
    "requisitos_proveedor": {
      "habilitaciones": ["RNP de Servicios vigente"],
      "admite_consorcio": null,
      "experiencia_minima": "No consta en el extracto",
      "documentos_acreditacion": [
        "RNP vigente en Servicios",
        "Curriculum Vitae documentado del personal propuesto"
      ],
      "certificaciones_especificas": []
    },
    "riesgos_contractuales": {
      "penalidad_formula": null,
      "plataforma_provee": "cliente",
      "clausulas_criticas": [
        {
          "impacto": "alto",
          "clausula": "Perfil específico del especialista",
          "descripcion": "Riesgo de descalificación si el TDR exige certificaciones o experiencia específica en Invierte.pe / OSCE no disponible de inmediato."
        }
      ],
      "penalidad_factor_f": null,
      "penalidad_tope_pct": 10,
      "propiedad_materiales": "cliente"
    },
    "estructura_contractual": {
      "entregables": [
        {
          "nombre": "Informes de monitoreo y soporte técnico",
          "plazo_dias": 30,
          "descripcion": "Reportes periódicos sobre la gestión y avance de contratos e inversiones del programa CUI N° 2505321.",
          "plazo_referencia": "desde_notificacion",
          "riesgo_penalidad": "medio"
        }
      ]
    }
  },
  "tdr_chars": 1253,
  "tdr_fuente": "chunks",
  "contrato_id": 93277,
  "techo_soles": 42800
}
```

## 8. Estado posterior

- Fila de `analisis_contrato` para 93277: **eliminada**.
- El gate corregido ya no lo regenera mientras no exista TDR real
  (`tdr_texto >= 200` o chunks `fuente='pdf'`).
- El contrato **sigue sin TDR**: su anexo es un `.rar` real
  (`Requerimiento.rar`, mime `application/x-compressed`), y el soporte de
  contenedores es una tarea aparte. Su `req_url` sigue en `sin_pdf`.
- Los 3 chunks `fuente='api'` **no se tocaron**: son correctos como metadatos
  de ficha, el problema era que habilitaran el análisis.
