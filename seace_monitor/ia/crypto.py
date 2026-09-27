"""Cifrado de claves de proveedores IA — contrato IA-001.

Algoritmo: AES-256-GCM. Clave maestra: 32 bytes en base64 en el secreto
``IA_MASTER_KEY`` de cada entorno participante (Supabase EF, Worker, Python).

Formato interoperable (idéntico en Python, Deno y WebCrypto del Worker)::

    v1.<base64(iv)>.<base64(ciphertext || tag)>

IV de 12 bytes aleatorio por cifrado; tag GCM de 16 bytes anexado al final
del ciphertext — disposición nativa de ``cryptography`` y de WebCrypto.

Rotación: descifrar todas las ``clave_cifrada`` con la clave vieja y re-cifrar
con la nueva dentro de una transacción; sustituir ``IA_MASTER_KEY`` en los
tres entornos en la misma ventana. Si la clave maestra se pierde, las claves
son irrecuperables: debe reingresarse cada API key y re-cifrar.
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = "v1"
_IV_BYTES = 12
_TAG_BYTES = 16
_KEY_BYTES = 32


def decodificar_master_key(master_b64: str) -> bytes:
    """Valida y decodifica la clave maestra base64 (32 bytes)."""
    raw = base64.b64decode(master_b64.strip(), validate=True)
    if len(raw) != _KEY_BYTES:
        raise ValueError("IA_MASTER_KEY debe ser base64 de 32 bytes")
    return raw


def cifrar_clave(clave: str, master_b64: str, iv: bytes | None = None) -> str:
    """Cifra una API key; devuelve 'v1.<b64iv>.<b64(ct||tag)>'."""
    if not clave:
        raise ValueError("clave vacía")
    key = decodificar_master_key(master_b64)
    nonce = iv if iv is not None else os.urandom(_IV_BYTES)
    if len(nonce) != _IV_BYTES:
        raise ValueError("iv debe ser de 12 bytes")
    ct = AESGCM(key).encrypt(nonce, clave.encode("utf-8"), None)
    return f"{VERSION}.{base64.b64encode(nonce).decode()}.{base64.b64encode(ct).decode()}"


def descifrar_clave(blob: str, master_b64: str) -> str:
    """Descifra un blob 'v1.<b64iv>.<b64(ct||tag)>'; ValueError si inválido."""
    key = decodificar_master_key(master_b64)
    try:
        version, iv_b64, ct_b64 = blob.split(".")
    except ValueError:
        raise ValueError("formato de clave_cifrada inválido") from None
    if version != VERSION:
        raise ValueError(f"versión de cifrado no soportada: {version}")
    iv = base64.b64decode(iv_b64, validate=True)
    ct = base64.b64decode(ct_b64, validate=True)
    if len(iv) != _IV_BYTES or len(ct) < _TAG_BYTES + 1:
        raise ValueError("blob de clave_cifrada corrupto")
    return AESGCM(key).decrypt(iv, ct, None).decode("utf-8")


def enmascarar_clave(clave: str) -> str:
    """Máscara para UI/auditoría: primeros 3 + últimos 4 caracteres."""
    if len(clave) <= 7:
        return "..."
    return f"{clave[:3]}...{clave[-4:]}"
