# CRITERIOS DE DECISIÓN — ENERTRONIC

> Inteligencia de negocio del asesor de licitaciones SEACE Monitor.
> Define cómo la IA evalúa, puntúa, prioriza y recomienda cada contrato.
> Este documento es la FUENTE DE VERDAD del scoring; el código lo implementa, no lo redefine.

---

## 0. Principio rector (regla de oro)

**La IA rankea y aconseja; NUNCA oculta. El humano decide.**

- Ningún contrato se descarta por un solo factor negativo. Se evalúa el PAQUETE completo.
- El score ordena por conveniencia, pero **todo es accesible** (la joya puede estar en el puesto 47).
- La IA prepara el análisis; **Rolando/ENERTRONIC pone el número final** de cotización.

---

## 1. Clasificación de rubros (qué tan "nuestro" es el contrato)

ENERTRONIC es generalista de TI —evalúa TODO lo tecnológico— pero prioriza donde
pone su IP y gana mejor margen. El hardware NUNCA se descarta; solo pesa menos.

| Nivel | Línea de negocio | Peso | Justificación |
|---|---|---|---|
| ⭐⭐⭐ **NÚCLEO** | Tokens de IA · Machine Learning · Servicios Cloud (AWS/Azure/GCP) · Desarrollo de software · Telemetría/OT | Máximo | IP propia, margen alto y escalable, zona de dominio de ENERTRONIC |
| ⭐⭐ **ADYACENTE** | Integraciones · Datos/BD · Digital twin · Automatización | Alto | Se ejecuta bien, buen margen |
| ⭐ **OPORTUNISTA** | Soporte técnico · Redes · Licencias · Ciberseguridad puntual | Medio | Se evalúa si las condiciones (paga/plazo) son buenas |
| ◐ **MARGINAL** | Reventa de hardware / compra de equipos | Bajo (no descartado) | Margen de intermediario; se muestra por si paga excelente ("no perder de vista el mercado") |

**Regla:** software (IA/cloud/ML/desarrollo) > hardware, **pero todo se evalúa y todo aparece en el ranking.**

---

## 2. Factores de evaluación (qué pondera el score)

La IA calcula un **score de conveniencia (0–100)** por contrato, ponderando en conjunto:

| Factor | Preferencia ENERTRONIC | Cómo influye en el score |
|---|---|---|
| **Encaje de rubro** | Núcleo > Adyacente > Oportunista > Marginal | Mayor peso si cae en el núcleo (IA/cloud/ML/desarrollo) |
| **Encaje técnico** | Que ENERTRONIC califique (perfil, experiencia, RNP) | Si NO califica → marcar en rojo / bajar fuerte. Si califica justo → nota amarilla |
| **Modalidad** | Remoto preferido; presencial OK si paga muy bien | Remoto suma; presencial resta un poco — pero un margen alto lo compensa |
| **Forma de pago** | 1 armada ideal; a más armadas, peor | Pago único y rápido suma; 13 armadas mensuales resta (caja amarrada) |
| **Margen estimado** | Cuanto mayor, mejor. Techo del contrato: ≤ 8 UIT (~S/42,800 en 2026) | El gran contrapeso: margen alto perdona presencial o armadas |
| **Plazo** | Proyectos que no amarren recursos de más | Plazo largo (ej. 365 días) resta algo si el pago también es lento |
| **Vigencia / urgencia** | Que esté abierto y con tiempo para postular | Si cierra hoy/mañana → marcar URGENTE. Si ya cerró → fuera del ranking activo |
| **Penalidades / riesgo** | Menor riesgo mejor | Penalidades duras o cláusulas riesgosas restan |

**El balance manda:** un presencial que paga excelente puede rankear alto; un remoto de margen mínimo puede rankear bajo. Nada es filtro absoluto (salvo no calificar técnicamente).

---

## 3. Estimación económica (cómo la IA calcula el margen)

Como los TDR muchas veces NO muestran el monto exacto, la IA estima con este marco:

1. **Techo conocido:** todo contrato ≤ 8 UIT (~S/42,800). Es el máximo posible.
2. **Ubicación en el rango:** la IA estima dónde cae ESTE contrato usando pistas del TDR:
   - Experiencia mínima pedida (ej. piden S/40k facturados ≈ contrato ~S/40k).
   - Cantidad de recursos / infraestructura / plazo.
3. **Costo estimado del servicio:** precios de mercado (la IA busca) según lo que pide el TDR.
4. **Margen = valor estimado − costo estimado.**
5. **SIEMPRE con supuestos explícitos.** La IA declara lo que NO sabe. Nunca da un número seco como si fuera certeza.

### Si el margen sale ajustado o negativo → análisis de optimización
La IA no dice solo "no". Añade cómo mejorar el margen, p. ej.:
- Dimensionar instancias más chicas / compromiso anual (−30% en cloud).
- Subcontratar o reusar infraestructura existente.
- Negociar alcance.
> Ejemplo real (SUTRAN cloud): "Valor ~S/40k, costo nube ~S/40–57k/año → margen negativo.
> Recomendación: NO, salvo optimizar instancias y usar compromiso anual. Con eso el costo
> baja a ~S/28k y el margen se vuelve viable."

---

## 4. Veredicto por contrato (lo que la IA concluye)

Cada contrato recibe un veredicto claro, con su razonamiento:

- 🟢 **RECOMENDADO** — encaja en núcleo/adyacente, margen sano, condiciones favorables (remoto, pago rápido).
- 🟡 **EVALUAR / AJUSTABLE** — bueno pero con un pero (margen apretado, presencial, plazo largo). Incluye cómo mejorarlo.
- 🔴 **NO RECOMENDADO** — no calificas técnicamente, o margen negativo sin forma clara de mejorar.
- ⚡ **URGENTE** — cualquiera de los anteriores que cierre hoy/mañana (bandera de tiempo).

El veredicto SIEMPRE explica el porqué (rubro, margen, modalidad, pago, plazo).

---

## 5. Presentación (cómo se muestra — clave: no ocultar nada)

### Brief diario (resumen matutino)
- **Top 15** oportunidades del día, ordenadas por score.
- Foco en núcleo (IA/cloud/ML/desarrollo), vigentes, y las que cierran pronto.
- Cada una: rubro, score, condiciones clave, veredicto.

### Ranking completo expandible (el humano elige cuánto ver)
- Expandible: **top 15 → 50 → 100 → 500 → 1000 → todos.**
- Ordenado por score, pero **NUNCA oculta** una oportunidad. La joya del puesto 47 debe ser alcanzable.

### Filtros sobre el ranking (para navegar rápido)
- Por **rubro** (núcleo, cloud, IA, desarrollo, hardware…).
- Por **fecha de cierre** (hoy, esta semana, este mes).
- Por **modalidad** (remoto / presencial).
- Por **estado** (vigente / en evaluación).
- Por **veredicto** (solo recomendados, solo ajustables…).
> El ranking ordena; los filtros dejan llegar a la joya del puesto 47 en segundos.

---

## 6. Workflow de análisis por contrato (clic → despliegue)

Al hacer clic en un contrato, la IA despliega su análisis (usando el TDR del RAG):

1. **Encaje:** ¿Es núcleo/adyacente/marginal? ¿ENERTRONIC califica (perfil, experiencia, RNP)?
2. **Condiciones:** modalidad, forma de pago, plazo, penalidades — extraídas del TDR.
3. **Economía:** valor estimado (vs techo 8 UIT) + costo estimado + margen, con supuestos visibles.
4. **Veredicto:** 🟢/🟡/🔴/⚡ con el razonamiento.
5. **Optimización** (si aplica): cómo mejorar el margen.
6. **Chat de mejora:** conversar ("¿y si uso instancias chicas?", "¿y si subo a S/40k?") y recalcular.

---

## 7. Capacidad y realismo

- El brief diario muestra 15, pero ENERTRONIC decide (por eso el ranking es expandible a 1000+).
- No se asume una capacidad máxima fija que oculte oportunidades — el humano gestiona su carga.

---

## 8. Lo que la IA NUNCA debe hacer

- ❌ Dar un número de cotización seco como si fuera certeza (sin mostrar supuestos).
- ❌ Ocultar/descartar un contrato por un solo factor (salvo no calificar técnicamente).
- ❌ Decidir por Rolando el número final de cotización.
- ❌ Inventar el monto del contrato: si no lo sabe, lo estima con pistas y lo declara como estimación.

---

*Documento vivo — se afina usándolo. Versión 1, definida con Rolando (ENERTRONIC).*
