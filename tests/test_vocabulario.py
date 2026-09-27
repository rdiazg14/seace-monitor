"""Caracterización de vocabulario.py (QA-001).

Motor de aprendizaje de keywords que consume `classification/` al registrar
señales: cubre normalización, extracción de término (score léxico),
coincidencia tipo-A por Levenshtein/prefijo, cobertura exact/tipo_a/libre y
persistencia de candidatas con Supabase falso. Las salidas reproducen el
comportamiento observado actual — incluidas las peculiaridades documentadas
(singularización 'antiviru', recorte 'network attached') — para detectar
regresiones durante IA-005, no para redefinirlas aquí.
"""

import pytest

from vocabulario import (
    clasificar_cobertura,
    es_tipo_a,
    extraer_senal_item,
    extraer_termino,
    levenshtein,
    madre_tipo_a,
    normalizar,
    registrar_candidata,
    registrar_desde_items,
    senal_es_keyword_activa,
    termino_valido_activar,
    cargar_pistas,
)


# --- helpers -----------------------------------------------------------

def kw(keyword, categoria="seguridad", tipo="incluye", activa=True):
    return {
        "categoria": categoria,
        "keyword": keyword,
        "tipo": tipo,
        "activa": activa,
    }


class _Q:
    """Cadena PostgREST falsa: registra filtros y devuelve data sembrada."""

    def __init__(self, supa, tabla, op="select"):
        self.supa = supa
        self.tabla = tabla
        self.op = op
        self.filtros = []
        self.payload = None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self.filtros.append(("eq", col, val))
        return self

    def gte(self, col, val):
        self.filtros.append(("gte", col, val))
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def insert(self, payload):
        self.op = "insert"
        self.payload = payload
        self.supa.escrituras.append((self.tabla, "insert", payload))
        return self

    def update(self, payload):
        self.op = "update"
        self.payload = payload
        return self

    def execute(self):
        if self.op == "insert":
            return type("R", (), {"data": [self.payload]})()
        if self.op == "update":
            self.supa.escrituras.append(
                (self.tabla, "update", self.payload, list(self.filtros))
            )
            return type("R", (), {"data": []})()
        rows = self.supa.data.get(self.tabla, [])
        for kind, col, val in self.filtros:
            if kind == "eq":
                rows = [r for r in rows if r.get(col) == val]
            elif kind == "gte":
                rows = [r for r in rows if (r.get(col) or 0) >= val]
        return type("R", (), {"data": rows})()


class FakeSupa:
    def __init__(self, data=None):
        self.data = data or {}
        self.escrituras = []

    def table(self, nombre):
        return _Q(self, nombre)


# --- normalización -----------------------------------------------------

def test_normalizar_acentos_caso_y_puntuacion():
    assert normalizar("  Adquisición de LICENCIAS—Empresa, S.A.! ") == (
        "adquisicion de licencias empresa s a"
    )
    assert normalizar("") == ""
    assert normalizar(None) == ""


# --- extraer_termino: salidas reales observadas ------------------------

@pytest.mark.parametrize(
    "senal,esperado",
    [
        (
            "Adquisicion de licencias de antivirus para la municipalidad",
            "antiviru",  # singularización corta la 's' final: comportamiento actual
        ),
        (
            "Servicio de mantenimiento preventivo de equipos de computo",
            "equipo de computo",
        ),
        (
            "Suscripcion a plataforma de videovigilancia anual",
            "plataforma de videovigilancia anual",
        ),
        ("Compra de laptops core i7 16gb ram", "laptop core i7 ram"),
        (
            "Implementacion de sistema de gestion documentaria",
            "sistema de gestion documentaria",
        ),
        ("Redes y comunicaciones", "redes y comunicacion"),
        ("Provision de internet dedicado 100 mbps", "internet dedicado"),
        (
            "Soporte tecnico especializado en redes nas network attached storage",
            "network attached",  # regla nas+network recorta a 2 palabras
        ),
    ],
)
def test_extraer_termino_nucleo(senal, esperado):
    assert extraer_termino(senal) == esperado


@pytest.mark.parametrize(
    "senal",
    [
        "",
        "servicio",          # genérico solo
        "Adquisicion de software",  # queda genérico tras strip
        "para la",           # solo prefijos vacíos
        "licencia",          # 1 palabra <=6 chars no en _CORTOS_OK
    ],
)
def test_extraer_termino_no_extraible(senal):
    assert extraer_termino(senal) is None


def test_extraer_termino_nucleo_keyword_activa():
    # Una keyword incluye activa contenida en la señal acorta el término al núcleo.
    kws = [kw("firewall perimetral")]
    assert (
        extraer_termino(
            "Adquisicion de firewall perimetral empresarial",
            categoria="seguridad",
            keywords=kws,
        )
        == "firewall perimetral"
    )


def test_extraer_termino_keyword_inactiva_no_recorta():
    kws = [kw("firewall perimetral", activa=False)]
    out = extraer_termino(
        "Adquisicion de firewall perimetral empresarial",
        categoria="seguridad",
        keywords=kws,
    )
    assert out != "firewall perimetral"


# --- termino_valido_activar ---------------------------------------------

@pytest.mark.parametrize(
    "termino,ok",
    [
        ("firewall perimetral", True),
        ("vpn", False),         # 3 chars < MIN_CHARS_TERMINO (4)
        ("abc", False),
        ("servicio de mantenimiento de equipos de computo", False),  # >40 chars
        ("para el servicio", False),  # empieza con vacía
        ("a b c d e f", False),  # >4 palabras
        ("", False),
    ],
)
def test_termino_valido_activar(termino, ok):
    assert termino_valido_activar(termino) is ok


# --- levenshtein ---------------------------------------------------------

def test_levenshtein_basico():
    assert levenshtein("abc", "abc") == 0
    assert levenshtein("", "abc") == 3
    assert levenshtein("antiviru", "antivirus") == 1
    assert levenshtein("kitten", "sitting") == 3


# --- es_tipo_a -----------------------------------------------------------

def test_tipo_a_levenshtein():
    ok, ev = es_tipo_a("antiviru", "antivirus")
    assert ok and ev["regla"] == "levenshtein" and ev["levenshtein"] == 1


def test_tipo_a_prefijo_corto():
    # 'firewal' ya cae en la regla levenshtein (dist 1); el prefijo se ejerce
    # cuando la distancia supera 2 pero el tramo corto cubre >=60% de la madre.
    ok, ev = es_tipo_a("firew", "firewall")  # dist 3, 5/8 = 62,5%
    assert ok and ev["regla"] == "prefijo_sufijo" and ev["ratio_madre"] == 0.625
    ok, ev = es_tipo_a("ewall", "firewall")  # también por sufijo
    assert ok and ev["regla"] == "prefijo_sufijo"


def test_tipo_a_rechazos():
    assert es_tipo_a("antivirus", "antivirus") == (False, {})  # iguales
    assert es_tipo_a("", "antivirus") == (False, {})
    ok, ev = es_tipo_a("firewall", "antivirus")
    assert not ok and ev["levenshtein"] == 9
    # prefijo demasiado corto respecto a la madre (<60%)
    assert es_tipo_a("desa", "desarrollo organizacional completo")[0] is False


# --- senal_es_keyword_activa ----------------------------------------------

def test_senal_es_keyword_activa_reglas():
    kws = [
        kw("antivirus"),
        kw("firewall", activa=False),
        kw("backup", categoria="datos"),
        kw("switch", tipo="excluye"),
    ]
    assert senal_es_keyword_activa(" Antivirus ", "seguridad", kws) is True
    assert senal_es_keyword_activa("", "seguridad", kws) is True  # vacía = activa
    assert senal_es_keyword_activa("firewall", "seguridad", kws) is False  # inactiva
    assert senal_es_keyword_activa("backup", "seguridad", kws) is False  # otra cat
    assert senal_es_keyword_activa("switch", "seguridad", kws) is False  # excluye
    assert senal_es_keyword_activa("router", "seguridad", kws) is False  # no listada


# --- madre_tipo_a ----------------------------------------------------------

def test_madre_tipo_a_mejor_score():
    # El score ordena por (levenshtein, len(keyword)): gana la madre más cercana.
    kws = [
        kw("antivirus corporativo avanzado"),
        kw("antivirus"),
    ]
    madre, ev = madre_tipo_a("antiviru", "seguridad", kws)
    assert madre["keyword"] == "antivirus" and ev["levenshtein"] == 1


def test_madre_tipo_a_sin_match():
    madre, ev = madre_tipo_a("teclado", "seguridad", [kw("antivirus")])
    assert madre is None and ev == {}


# --- clasificar_cobertura -------------------------------------------------

def test_clasificar_cobertura_tres_vias():
    kws = [kw("antivirus")]
    assert clasificar_cobertura("antivirus", "seguridad", kws)[0] == "exact"
    assert clasificar_cobertura("antiviru", "seguridad", kws)[0] == "tipo_a"
    assert clasificar_cobertura("teclado", "seguridad", kws)[0] == "libre"
    assert clasificar_cobertura("", "seguridad", kws) == ("libre", None, {})


# --- registrar_candidata ---------------------------------------------------

def test_registrar_candidata_skip_sin_termino():
    supa = FakeSupa()
    assert (
        registrar_candidata(supa, senal="servicio", categoria="it", contrato_id=1, keywords=[])
        == "skip"
    )
    assert supa.escrituras == []


def test_registrar_candidata_skip_categoria_ninguna():
    supa = FakeSupa()
    assert (
        registrar_candidata(
            supa, senal="firewall perimetral", categoria="ninguna",
            contrato_id=1, keywords=[],
        )
        == "skip"
    )


def test_registrar_candidata_skip_si_ya_cubierta():
    kws = [kw("antivirus")]
    supa = FakeSupa()
    # 'antiviru' -> termino; cobertura tipo_a -> skip
    assert (
        registrar_candidata(
            supa, senal="antiviru", categoria="seguridad",
            contrato_id=1, keywords=kws,
        )
        == "skip"
    )


def test_registrar_candidata_nueva_inserta():
    supa = FakeSupa({"keyword_candidatas": []})
    r = registrar_candidata(
        supa, senal="Compra de laptops core i7", categoria="hardware",
        contrato_id=42, keywords=[],
    )
    assert r == "nueva"
    tabla, op, payload = supa.escrituras[0]
    assert tabla == "keyword_candidatas" and op == "insert"
    assert payload["estado"] == "nueva" and payload["contratos"] == [42]
    assert payload["ejemplo_contrato_id"] == 42


def test_registrar_candidata_inc_actualiza_contador():
    supa = FakeSupa({
        "keyword_candidatas": [
            {"id": 7, "senal": "laptop core i7 ram", "categoria_propuesta": "hardware",
             "estado": "nueva", "veces_vista": 2, "contratos": [1, 2]},
        ]
    })
    r = registrar_candidata(
        supa, senal="Compra de laptops core i7 16gb ram", categoria="hardware",
        contrato_id=3, keywords=[],
    )
    assert r == "inc"
    tabla, op, payload, filtros = supa.escrituras[0]
    assert op == "update" and payload["veces_vista"] == 3
    assert payload["contratos"] == [1, 2, 3]
    assert ("eq", "id", 7) in filtros


def test_registrar_candidata_skip_rechazada():
    supa = FakeSupa({
        "keyword_candidatas": [
            {"id": 7, "senal": "laptop core i7 ram", "categoria_propuesta": "hardware",
             "estado": "rechazada", "veces_vista": 9, "contratos": []},
        ]
    })
    assert (
        registrar_candidata(
            supa, senal="Compra de laptops core i7 16gb ram", categoria="hardware",
            contrato_id=3, keywords=[],
        )
        == "skip"
    )
    assert supa.escrituras == []


# --- extraer_senal_item -----------------------------------------------------

def test_extraer_senal_item_prioridad_p2_verificada():
    it = {
        "p1": {"categoria": "redes", "senal": "s1", "senal_verificada": True},
        "p2": {"categoria": "seguridad", "senal": "s2", "senal_verificada": True},
    }
    assert extraer_senal_item(it) == ("s2", "seguridad")


def test_extraer_senal_item_cae_a_p1_si_p2_no_verifica():
    it = {
        "p1": {"categoria": "redes", "senal": "s1", "senal_verificada": True},
        "p2": {"categoria": "seguridad", "senal": "s2", "senal_verificada": False},
    }
    assert extraer_senal_item(it) == ("s1", "redes")


def test_extraer_senal_item_descartes():
    assert extraer_senal_item({}) == (None, None)
    assert extraer_senal_item({"p2": {"categoria": "ninguna", "senal": "x", "senal_verificada": True}}) == (None, None)
    assert extraer_senal_item({"p1": {"categoria": "redes", "senal": "", "senal_verificada": True}}) == (None, None)


# --- registrar_desde_items ---------------------------------------------------

def test_registrar_desde_items_cuenta_nuevas_e_incs():
    supa = FakeSupa({
        "it_keywords": [],
        "keyword_candidatas": [
            {"id": 1, "senal": "laptop core i7 ram", "categoria_propuesta": "hardware",
             "estado": "nueva", "veces_vista": 1, "contratos": [9]},
        ],
    })
    items = [
        {"id": 10, "p2": {"categoria": "hardware", "senal": "Compra de laptops core i7 16gb ram", "senal_verificada": True}},
        {"id": 11, "p1": {"categoria": "software", "senal": "Suscripcion a plataforma de videovigilancia anual", "senal_verificada": True}},
        {"id": "x", "p1": {"categoria": "software", "senal": "ignorado", "senal_verificada": True}},
        {"p1": {"categoria": "software", "senal": "sin id", "senal_verificada": True}},
    ]
    nuevas, incs = registrar_desde_items(supa, items)
    assert (nuevas, incs) == (1, 1)


# --- cargar_pistas -------------------------------------------------------------

def test_cargar_pistas_medida_y_nueva_con_veces():
    supa = FakeSupa({
        "keyword_candidatas": [
            {"senal": "firewall ng", "categoria_propuesta": "seguridad",
             "veces_vista": 5, "estado": "medida"},
            {"senal": "thin client", "categoria_propuesta": "hardware",
             "veces_vista": 3, "estado": "nueva"},
            {"senal": "visto una vez", "categoria_propuesta": "otra",
             "veces_vista": 1, "estado": "nueva"},
            {"senal": "aprobada", "categoria_propuesta": "otra",
             "veces_vista": 9, "estado": "aprobada_admin"},
        ]
    })
    out = cargar_pistas(supa)
    assert "firewall ng -> seguridad" in out
    assert "thin client -> hardware" in out
    assert "visto una vez" not in out
    assert "aprobada" not in out


def test_cargar_pistas_vacia_y_error():
    assert cargar_pistas(FakeSupa({"keyword_candidatas": []})) == ""

    class SupaRoto:
        def table(self, _n):
            raise RuntimeError("caído")

    assert cargar_pistas(SupaRoto()) == ""
