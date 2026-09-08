-- C3: cola de revision admin (clasificacion_pendiente).
-- IDEMPOTENTE: migraciones_datos.nombre=c3_clasificacion_pendiente.
-- SELECT solo admin (es_admin). Escritura: service_role / pipeline / Edge Function.
-- Ejecutar: uv run python scripts/run_sql.py docs/c3_clasificacion_pendiente.sql

BEGIN;

CREATE TABLE IF NOT EXISTS public.clasificacion_pendiente (
  id              bigserial PRIMARY KEY,
  contrato_id     bigint NOT NULL
                    REFERENCES public.contratos(id) ON DELETE CASCADE,
  categoria_p1    text,
  categoria_p2    text,
  origen          text,
  votos           jsonb,
  estado          text NOT NULL
                    CHECK (estado IN ('pendiente','aprobada','rechazada','observacion')),
  nota            text,
  titulo          text,
  artefacto       text,
  creado_utc      timestamptz NOT NULL DEFAULT now(),
  resuelto_utc    timestamptz,
  resuelto_por    uuid,
  UNIQUE (contrato_id)
);

COMMENT ON TABLE public.clasificacion_pendiente IS
  'C3 cola de revision. Gemini escribe pendientes. Admin aprueba/rechaza via Edge Function. Nunca el browser.';

COMMENT ON COLUMN public.clasificacion_pendiente.resuelto_por IS
  'JWT sub del admin. Escritura: Edge Function service_role.';

CREATE INDEX IF NOT EXISTS clasificacion_pendiente_estado_idx
  ON public.clasificacion_pendiente (estado);

ALTER TABLE public.clasificacion_pendiente ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.clasificacion_pendiente FROM anon, authenticated;
GRANT SELECT ON TABLE public.clasificacion_pendiente TO authenticated;

DROP POLICY IF EXISTS clasificacion_pendiente_select_admin ON public.clasificacion_pendiente;
CREATE POLICY clasificacion_pendiente_select_admin
  ON public.clasificacion_pendiente
  FOR SELECT
  TO authenticated
  USING (public.es_admin());

COMMENT ON POLICY clasificacion_pendiente_select_admin ON public.clasificacion_pendiente IS
  'Solo perfiles.rol = admin (es_admin). Escritura: service_role / pipeline.';

INSERT INTO public.clasificacion_pendiente (
  contrato_id, categoria_p1, categoria_p2, origen, votos, estado,
  nota, titulo, artefacto, creado_utc
)
SELECT
  (elem->>'id')::bigint,
  elem->>'p1',
  elem->>'p2',
  elem->>'origen',
  elem->'votos',
  elem->>'estado',
  CASE
    WHEN elem->>'nota' IS NOT NULL AND elem->>'keyword' IS NOT NULL
      THEN (elem->>'keyword') || ': ' || (elem->>'nota')
    ELSE COALESCE(elem->>'nota', elem->>'keyword')
  END,
  elem->>'titulo',
  elem->>'artefacto',
  COALESCE('2026-09-08T01:11:05.930532+00:00'::timestamptz, now())
FROM jsonb_array_elements($c3seed$
[
  {
    "id": 90331,
    "origen": "gemini_directo_sin_consenso",
    "p1": "Redes/cableado",
    "p2": null,
    "escrita": "Redes/cableado",
    "titulo": "Contratar un servicio integral de comunicación privada para Orcopampa, Huancarama y Acarí",
    "artefacto": "clasificar_gemini.py camino_directo 2026-09-01",
    "estado": "pendiente"
  },
  {
    "id": 90140,
    "origen": "consenso_inestable",
    "p1": "Firma digital",
    "p2": "Firma digital",
    "escrita": null,
    "titulo": "SERVICIO DE AUTENTICACION Y SEGURIDAD DE SERVIDORES DE RED MEDIANTE LA PROVISION, INSTALACION Y CONFIGURACION DE...",
    "artefacto": "consenso_it_20260903-043502.json",
    "estado": "pendiente",
    "votos": {
      "propuestas_it_20260903-040545.json": "Firma digital",
      "propuestas_it_20260903-041040.json": "Ciberseguridad",
      "propuestas_it_20260903-041553.json": "Firma digital"
    }
  },
  {
    "id": 79006,
    "origen": "discrepa_es_it",
    "p1": "Redes/cableado",
    "p2": "ninguna",
    "escrita": null,
    "titulo": "ADQUISICION DE ANALIZADOR DE REDES EN EL MARCO DE LA IOARR 2537265 ADQUISICION DE EQUIPAMIENTO DE AULA, EQUIPAMIENTO...",
    "artefacto": "propuestas_it_20260906-030714.json",
    "estado": "pendiente"
  },
  {
    "id": 74482,
    "origen": "discrepa_es_it",
    "p1": "Licencias",
    "p2": "ninguna",
    "escrita": null,
    "titulo": "CONTRATACIÓN DEL SERVICIO WEB DE CONSULTAS EN LISTAS INTERNACIONALES RESTRICTIVAS ESTABLECIDAS POR LA SBS Y...",
    "artefacto": "propuestas_it_20260903-031747.json",
    "estado": "pendiente"
  },
  {
    "id": 57882,
    "origen": "discrepa_intra_it",
    "p1": "Desarrollo software",
    "p2": "IA/analytics",
    "escrita": "Desarrollo software",
    "titulo": "CONTRATACIÓN DE REPORTE Y ESTADISTICO DE CONTROL DE ACCESOS AL SISTEMA DE DA00 EN LA PLATAFORMA DE MAINFRAME",
    "artefacto": "propuestas_it_20260903-041553.json",
    "estado": "pendiente"
  },
  {
    "id": 34039,
    "origen": "consenso_inestable",
    "p1": "ninguna",
    "p2": null,
    "escrita": null,
    "titulo": "ADQUISICION DE COLECTOR DE DATOS",
    "artefacto": "consenso_it_20260903-043502.json",
    "estado": "pendiente",
    "votos": {
      "propuestas_it_20260903-040545.json": "Hardware",
      "propuestas_it_20260903-041040.json": "ninguna",
      "propuestas_it_20260903-041553.json": "ninguna"
    }
  },
  {
    "id": 32378,
    "origen": "discrepa_intra_it",
    "p1": "Redes/cableado",
    "p2": "Hardware",
    "escrita": "Redes/cableado",
    "titulo": "SERVICIO DE ADECUACION Y EQUIPAMENTO DE DATA CENTER ALTERNO",
    "artefacto": "propuestas_it_20260903-041040.json",
    "estado": "pendiente"
  },
  {
    "id": 31971,
    "origen": "desempate_sin_evidencia",
    "p1": "Hardware",
    "p2": "Hardware",
    "escrita": null,
    "titulo": "ADQUISICION DE EQUIPAMIENTO PARA SEGURIDAD INSTITUCIONAL Y OTROS",
    "artefacto": "propuestas_it_20260903-041553.json",
    "estado": "pendiente"
  },
  {
    "id": 18971,
    "origen": "desempate_sin_evidencia",
    "p1": "Redes/cableado",
    "p2": "Redes/cableado",
    "escrita": null,
    "titulo": "SERVICIO DE MONTAJE DE CÁMARAS DE VIGILANCIA EN EL ALMACÉN DE LA UGEL CAJAMARCA - La contratación del servicio de...",
    "artefacto": "propuestas_it_20260906-030335.json",
    "estado": "pendiente"
  },
  {
    "id": 18267,
    "origen": "discrepa_es_it",
    "p1": "Ciberseguridad",
    "p2": "ninguna",
    "escrita": null,
    "titulo": "SERVICIO ESPECIALIZADO EN LA FORMULACION DEL ANALISIS DE IMPACTO DE NEGOCIO",
    "artefacto": "propuestas_it_20260906-030335.json",
    "estado": "pendiente"
  },
  {
    "id": 11415,
    "origen": "discrepa_es_it",
    "p1": "IA/analytics",
    "p2": "ninguna",
    "escrita": null,
    "titulo": "SERVICIO DE PROCESAMIENTO DE DATOS",
    "artefacto": "propuestas_it_20260906-030714.json",
    "estado": "pendiente"
  },
  {
    "id": 90891,
    "origen": "discrepa_intra_it",
    "p1": "Soporte tecnico",
    "p2": "Cloud/hosting",
    "escrita": "Soporte tecnico",
    "titulo": "SERVICIO ESPECIALIZADO DE SOPORTE Y ADMINISTRACIÓN DE LA PLATAFORMA DE VIRTUALIZACIÓN E INFRAESTRUCTURA ASOCIADA",
    "artefacto": "propuestas_it_20260903-040545.json",
    "estado": "pendiente"
  },
  {
    "id": 77731,
    "origen": "desempate_sin_evidencia",
    "p1": "IA/analytics",
    "p2": "IA/analytics",
    "escrita": null,
    "titulo": "SUSCRIPCION ANUAL A UNA PLATAFORMA EN LA NUBE",
    "artefacto": "propuestas_it_20260906-030335.json",
    "estado": "pendiente"
  },
  {
    "id": 70370,
    "origen": "discrepa_intra_it",
    "p1": "Desarrollo software",
    "p2": "Licencias",
    "escrita": "Desarrollo software",
    "titulo": "SERVICIO DE FACTURACION ELECTRONICA DE 75000 FOLIOS PARA LA DIGA",
    "artefacto": "propuestas_it_20260906-030714.json",
    "estado": "pendiente"
  },
  {
    "id": 18635,
    "origen": "consenso_inestable",
    "p1": "Hardware",
    "p2": "Hardware",
    "escrita": null,
    "titulo": "ADQUISICION DE EQUIPO DE CONTROL DE ACCESO BIOMETRICO",
    "artefacto": "consenso_it_20260903-043502.json",
    "estado": "pendiente",
    "votos": {
      "propuestas_it_20260903-040545.json": "ninguna",
      "propuestas_it_20260903-041040.json": "ninguna",
      "propuestas_it_20260903-041553.json": "Hardware"
    }
  },
  {
    "id": 91229,
    "origen": "consenso_inestable",
    "p1": "Licencias",
    "p2": "Ciberseguridad",
    "escrita": null,
    "titulo": "ADQUISICIÓN DE 380 LICENCIAS DE SOFTWARE ANTIVIRUS PARA LA UGEL 03",
    "artefacto": "consenso_it_20260903-043502.json",
    "estado": "pendiente",
    "votos": {
      "propuestas_it_20260903-040545.json": "Ciberseguridad",
      "propuestas_it_20260903-041040.json": "Licencias",
      "propuestas_it_20260903-041553.json": "Licencias"
    }
  },
  {
    "id": 78271,
    "origen": "discrepa_es_it",
    "p1": "Correo electronico",
    "p2": "ninguna",
    "escrita": null,
    "titulo": "Servicio de envío de mensajes de texto a través de SMS",
    "artefacto": "propuestas_it_20260906-030714.json",
    "estado": "pendiente"
  },
  {
    "id": 34405,
    "origen": "consenso_inestable",
    "p1": "ninguna",
    "p2": null,
    "escrita": null,
    "titulo": "ADQUISICION DE COLECTOR DE DATOS",
    "artefacto": "consenso_it_20260903-043502.json",
    "estado": "pendiente",
    "votos": {
      "propuestas_it_20260903-040545.json": "Hardware",
      "propuestas_it_20260903-041040.json": "ninguna",
      "propuestas_it_20260903-041553.json": "ninguna"
    }
  },
  {
    "id": 33395,
    "origen": "consenso_inestable",
    "p1": "Hardware",
    "p2": "Hardware",
    "escrita": null,
    "titulo": "ADQUISICION DE COLECTOR DE DATOS",
    "artefacto": "consenso_it_20260903-043502.json",
    "estado": "pendiente",
    "votos": {
      "propuestas_it_20260903-040545.json": "ninguna",
      "propuestas_it_20260903-041040.json": "Hardware",
      "propuestas_it_20260903-041553.json": "Hardware"
    }
  },
  {
    "id": 33303,
    "origen": "consenso_inestable",
    "p1": "Hardware",
    "p2": "Hardware",
    "escrita": null,
    "titulo": "ADQUISICION DE COLECTOR DE DATOS",
    "artefacto": "consenso_it_20260903-043502.json",
    "estado": "pendiente",
    "votos": {
      "propuestas_it_20260903-040545.json": "ninguna",
      "propuestas_it_20260903-041040.json": "Hardware",
      "propuestas_it_20260903-041553.json": "Hardware"
    }
  },
  {
    "id": 92055,
    "origen": "discrepa_intra_it",
    "p1": "Desarrollo software",
    "p2": "Licencias",
    "escrita": "Desarrollo software",
    "titulo": "Servicio de renovación del Sistema informático denominado \"Sistema para la gestion de riego - modulo POMDIH...",
    "artefacto": "propuestas_it_20260906-030714.json",
    "estado": "pendiente"
  },
  {
    "id": 87248,
    "origen": "discrepa_es_it",
    "p1": "Cloud/hosting",
    "p2": "ninguna",
    "escrita": null,
    "titulo": "REQ 2026004275 (21834) - Servicio especializado de Limpieza Técnica de Gabinetes de comunicaciones y Centros de Datos...",
    "artefacto": "propuestas_it_20260906-030335.json",
    "estado": "pendiente"
  },
  {
    "id": 86911,
    "origen": "c2_fase5b_limite_conocido",
    "keyword": "equipo multifuncional",
    "p1": "Hardware",
    "p2": null,
    "escrita": null,
    "titulo": "ADQUISICION DE 01 ESTABILIZADOR DE 3KVA PARA EQUIPO MULTIFUNCIONAL KYOCERA TASKALFA 6004i",
    "artefacto": "c2_fase5b_ajustes",
    "estado": "observacion",
    "nota": "mezcla impresoras con estabilizadores/repuestos; queda activa a proposito"
  },
  {
    "id": 87001,
    "origen": "c2_fase5b_limite_conocido",
    "keyword": "microforma",
    "p1": "Soporte tecnico",
    "p2": null,
    "escrita": null,
    "titulo": "Servicio de almacenamiento y custodia de microformas",
    "artefacto": "c2_fase5b_ajustes",
    "estado": "observacion",
    "nota": "roza digitalizacion/archivo, que excluimos a proposito; queda activa a proposito"
  },
  {
    "id": 7326,
    "origen": "c2_fase5b_limite_conocido",
    "keyword": "sistema de gestion documental",
    "p1": "Desarrollo software",
    "p2": null,
    "escrita": null,
    "titulo": "SERVICIO DE APOYO EN EL PROCESAMIENTO DE LA DOCUMENTACION QUE INGRESA POR EL SISTEMA DE GESTION DOCUMENTAL: MESA DE PARTES VIRTUAL Y FISICA.",
    "artefacto": "c2_fase5b_ajustes",
    "estado": "observacion",
    "nota": "el caso 7326 es locacion/apoyo de archivo, no desarrollo; queda activa a proposito"
  },
  {
    "id": 2989,
    "origen": "c2_fase5b_limite_conocido",
    "keyword": "transformacion digital",
    "p1": "Soporte tecnico",
    "p2": null,
    "escrita": null,
    "titulo": "Servicio de evaluacion estructural para la verificacion de la capacidad de carga para la adecuada Implementacion de mejoras en el centro de datos",
    "artefacto": "c2_fase5b_ajustes",
    "estado": "observacion",
    "nota": "el caso 2989 es evaluacion estructural; queda activa a proposito"
  }
]
$c3seed$::jsonb) AS elem
ON CONFLICT (contrato_id) DO NOTHING;

INSERT INTO public.migraciones_datos (nombre, filas_afectadas, detalle)
VALUES (
  'c3_clasificacion_pendiente',
  (SELECT count(*)::int FROM public.clasificacion_pendiente),
  jsonb_build_object(
    'tabla', 'clasificacion_pendiente',
    'semilla', 'data/revisar_categoria.json',
    'nota', 'SELECT es_admin; escritura service_role'
  )
)
ON CONFLICT (nombre) DO NOTHING;

COMMIT;
