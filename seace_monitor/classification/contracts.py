"""Contratos de categor?as, prompts y schemas de clasificaci?n Gemini."""

CATEGORIAS_IT = [
    "Firma digital",
    "IA/analytics",
    "Ciberseguridad",
    "Cloud/hosting",
    "Microsoft",
    "Oracle",
    "Base de datos/ERP",
    "Desarrollo software",
    "Licencias",
    "Soporte tecnico",
    "Redes/cableado",
    "Correo electronico",
    "Hardware",
]
CATEGORIA_NINGUNA = "ninguna"
ENUM_CATEGORIA = CATEGORIAS_IT + [CATEGORIA_NINGUNA]
CONF_ENUM = ("alta", "media", "baja")

RESPONSE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "categoria": {"type": "string", "enum": ENUM_CATEGORIA},
        },
        "required": ["id", "categoria"],
    },
}

RESPONSE_SCHEMA_P1 = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "senal": {"type": "string"},
            "categoria": {"type": "string", "enum": ENUM_CATEGORIA},
            "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
        },
        "required": ["id", "senal", "categoria", "confianza"],
        "propertyOrdering": ["id", "senal", "categoria", "confianza"],
    },
}

RESPONSE_SCHEMA_P2 = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "senal": {"type": "string"},
            "categoria": {"type": "string", "enum": ENUM_CATEGORIA},
        },
        "required": ["id", "senal", "categoria"],
        "propertyOrdering": ["id", "senal", "categoria"],
    },
}

# Una linea por categoria, derivada de IT_CATS (ingesta_completa.py).
DEF_CATEGORIAS = """\
- Firma digital: firma digital, certificado digital/electronico, token criptografico.
- IA/analytics: inteligencia artificial, LLM/GPT/Copilot, analytics, BI, big data, ML.
- Ciberseguridad: ciberseguridad, seguridad informatica/de la informacion, firewall, pentest.
- Cloud/hosting: infraestructura o plataforma en la nube contratada como servicio (IaaS/PaaS): servidor virtual, hosting, almacenamiento, capacidad de computo, servicios gestionados sobre AWS/Azure/GCP.
- Microsoft: Microsoft 365, Office 365, SharePoint, Exchange, Windows Server.
- Oracle: Oracle Database, Oracle EBS, PeopleSoft.
- Base de datos/ERP: motores SQL, data warehouse, SAP, ERP.
- Desarrollo software: creacion/implementacion de sistemas, aplicativos web o moviles, software a medida.
- Licencias: derecho de uso de software de terceros, sea perpetuo, por suscripcion o entregado como servicio en la nube (SaaS). Si lo que se compra es el derecho de uso de un producto de un tercero (Autodesk, Adobe, SOTI, ArcGIS, Microsoft 365 y similares), es Licencias aunque se entregue en la nube y aunque el titulo diga "cloud" o "suscripcion".
- Soporte tecnico: soporte tecnico, mantenimiento de software/sistemas, mesa de ayuda. Es sobre software, sistemas o infraestructura TI. El mantenimiento o reparacion FISICA de equipos de oficina (impresoras, fotocopiadoras, escaneres como aparato) NO es Soporte tecnico -> 'ninguna'.
- Redes/cableado: red de datos, cableado estructurado, switch, router, wifi, fibra optica.
- Correo electronico: correo o mensajeria electronica.
- Hardware: compra de EQUIPOS de computo (PC, laptop, impresora, monitor, disco, RAM, scanner, UPS de datacenter). NO son Hardware: los consumibles y suministros de esos equipos (toner, cartuchos, tinta, cintas, papel, etiquetas, rollos, repuestos genericos) aunque el texto nombre el equipo que los usa; ni los equipos de reprografia de oficina (fotocopiadora, duplicadora, mimeografo, guillotina); ni electrodomesticos, estabilizadores de oficina o aire acondicionado. Todo eso -> 'ninguna'.
- ninguna: el objeto NO es tecnologia de la informacion.
"""

SYSTEM_PROMPT_REGLAS = (
    "Eres un clasificador de contratos publicos peruanos para una "
    "empresa de TI (ENERTRONIC: IA, cloud, desarrollo de software, servicios TI). "
    "Clasificas cada contrato en UNA de 13 categorias IT, o 'ninguna' si NO es "
    "un contrato de tecnologia de la informacion.\n"
    "REGLA CRITICA: clasifica por el OBJETO real del contrato (que se compra o "
    "contrata), NO por el area que lo solicita. Un area de TI/Informatica que "
    "compra aire acondicionado, mobiliario, estabilizadores o pide personal "
    "administrativo NO es un contrato IT -> 'ninguna'. Un area no-TI que compra "
    "desarrollo de software SI es IT.\n"
    "Personal: locar o contratar a una persona (bachiller, ingeniero, locador, "
    "practicante, 'servicio de un profesional') NO es Desarrollo software aunque "
    "el titulo sea de sistemas/informatica. Desarrollo software es crear o "
    "implementar un sistema/aplicativo/software, no alquilar un profesional.\n"
    "Soporte tecnico es sobre software, sistemas o infraestructura TI. El "
    "mantenimiento o reparacion FISICA de equipos de oficina (impresoras, "
    "fotocopiadoras, escaneres como aparato) NO es Soporte tecnico -> 'ninguna'.\n"
    "Hardware es SOLO compra de EQUIPOS de computo (PC, laptop, impresora, "
    "monitor, disco, RAM, scanner, UPS de datacenter). NO son Hardware: los "
    "consumibles y suministros de esos equipos (toner, cartuchos, tinta, "
    "cintas, papel, etiquetas, rollos, repuestos genericos) aunque el texto "
    "nombre el equipo que los usa; ni los equipos de reprografia de oficina "
    "(fotocopiadora, duplicadora, mimeografo, guillotina); ni electrodomesticos, "
    "estabilizadores de oficina o aire acondicionado. Todo eso -> 'ninguna'.\n"
    "Frontera Licencias vs Cloud/hosting: si el objeto es el derecho de uso de un "
    "producto de software de un tercero, es Licencias, aunque se entregue en la "
    "nube. Cloud/hosting es solo cuando se contrata infraestructura o plataforma "
    "(computo, almacenamiento, servidores, servicios gestionados).\n"
    'La "senal" debe copiarse del texto de "descripcion", "objeto" o "item". El '
    'campo "cubso" es la familia del catalogo estatal, no describe lo que se '
    'compra: si la unica evidencia esta ahi, la confianza es "media" como maximo.\n'
    "Definicion breve de cada categoria:\n"
    f"{DEF_CATEGORIAS}"
    "Ante la duda entre una categoria IT y 'ninguna', prefiere 'ninguna' si el "
    "objeto no es claramente tecnologia (mejor no clasificar que clasificar mal).\n"
)

SYSTEM_PROMPT_JSON = (
    "Responde solo el JSON array del schema, un objeto por contrato de entrada."
)

BLOQUE_CONFIANZA_P1 = (
    "Por cada contrato devuelve tambien:\n"
    '- "senal": las palabras LITERALES del texto del contrato (descripcion, objeto o '
    "item) que justifican la categoria, copiadas tal cual, maximo 60 caracteres. "
    "No inventes ni parafrasees. Si no podes copiar palabras del texto que "
    'justifiquen la categoria, la confianza es "baja".\n'
    '- "confianza": "alta" si el texto nombra explicitamente el producto o servicio '
    'de la categoria; "media" si se infiere del contexto pero no esta nombrado; '
    '"baja" si podria ser otra categoria o \'ninguna\'.\n'
    'Para \'ninguna\' usa siempre confianza "alta" y senal "".\n'
)

LINEA_P2_CIEGO = (
    "Clasificas solo por el objeto del contrato. No tenes informacion de la entidad "
    "ni del area solicitante y no debes suponerla.\n"
)

SYSTEM_PROMPT = SYSTEM_PROMPT_REGLAS + SYSTEM_PROMPT_JSON
SYSTEM_PROMPT_P1 = SYSTEM_PROMPT_REGLAS + BLOQUE_CONFIANZA_P1 + SYSTEM_PROMPT_JSON
SYSTEM_PROMPT_P2 = SYSTEM_PROMPT_REGLAS + LINEA_P2_CIEGO + SYSTEM_PROMPT_JSON
