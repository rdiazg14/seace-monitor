"""Contrato de cifrado IA-001: AES-256-GCM interoperable.

Los vectores CANONICAL_* son los mismos que se verifican en
seace-ai-proxy/src/ia/crypto.test.ts; ambas implementaciones deben descifrar
los mismos blobs y producir salidas descifrables por la otra.
"""

import base64

import pytest

from seace_monitor.ia.crypto import (
    cifrar_clave,
    descifrar_clave,
    decodificar_master_key,
    enmascarar_clave,
)

MASTER = base64.b64encode(b"K" * 32).decode()
OTRA_MASTER = base64.b64encode(b"X" * 32).decode()

# Generado con IV determinista bytes(1..12): descifrable en TS y Deno.
CANONICAL_FIJO = "v1.AQIDBAUGBwgJCgsM.Z+MXfpSdffmEWcWepYpW1xttrkwTvpSNWjLzKFzpbu4="
CANONICAL_FIJO_PLANO = "sk-test-ABCD1234"
# Generado con IV aleatorio en Python; TS debe poder descifrarlo.
CANONICAL_ALEATORIO = "v1.nNRStWpBbjW8IM3m.54MzJQD48Pe2LNtKiU08HfoA3Urfrfq4MkXQMGZUi+I45A=="
CANONICAL_ALEATORIO_PLANO = "sk-live-Q9w8e7r6t5"


def test_roundtrip():
    blob = cifrar_clave("sk-cualquiera-123", MASTER)
    assert blob.startswith("v1.")
    assert descifrar_clave(blob, MASTER) == "sk-cualquiera-123"


def test_descifra_vectores_canonicos():
    assert descifrar_clave(CANONICAL_FIJO, MASTER) == CANONICAL_FIJO_PLANO
    assert descifrar_clave(CANONICAL_ALEATORIO, MASTER) == CANONICAL_ALEATORIO_PLANO


def test_iv_determinista_reproduce_canonico():
    iv = bytes(range(1, 13))
    assert cifrar_clave(CANONICAL_FIJO_PLANO, MASTER, iv) == CANONICAL_FIJO


def test_iv_aleatorio_por_defecto():
    assert cifrar_clave("k", MASTER) != cifrar_clave("k", MASTER)


def test_clave_incorrecta_falla():
    blob = cifrar_clave("sk-secreto", MASTER)
    with pytest.raises(Exception):
        descifrar_clave(blob, OTRA_MASTER)


@pytest.mark.parametrize(
    "blob",
    [
        "sin-puntos",
        "v2.AQIDBAUGBwgJCgsM.Z+MXfpSdffmEWcWepYpW1xttrkwTvpSNWjLzKFzpbu4=",
        "v1.AQIDBAUGBwgJCgsM.AA==",  # ct menor que tag
        "v1.@@@" ".AAA",
    ],
)
def test_blob_invalido(blob):
    with pytest.raises(ValueError):
        descifrar_clave(blob, MASTER)


def test_master_key_invalida():
    with pytest.raises(Exception):
        decodificar_master_key(base64.b64encode(b"corta").decode())


def test_clave_vacia_rechazada():
    with pytest.raises(ValueError):
        cifrar_clave("", MASTER)


def test_enmascarar():
    assert enmascarar_clave("sk-live-Q9w8e7r6t5") == "sk-...r6t5"
    assert enmascarar_clave("abc") == "..."
