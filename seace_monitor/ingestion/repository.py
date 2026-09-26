"""Persistencia de contratos y rechazos de ingesta."""

from __future__ import annotations

import time
from collections.abc import Callable

from seace_monitor.db import connect

from .models import id_contrato_de, payload_solo_datos

BATCH_SIZE = 500
COLS_CONTRATOS = [
    "id", "nro_contratacion", "descripcion_contrato", "objeto", "descripcion",
    "entidad", "estado", "fecha_publica", "fecha_ini_cotizacion",
    "fecha_fin_cotizacion", "tipo_cotizacion", "cotizar",
]


def registrar_rechazo(client, payload: dict, motivo: str, origen: str = "ingesta") -> None:
    if client is None:
        print(f"  [rechazo] (sin supabase) {motivo[:180]}", flush=True)
        return
    data = (
        payload_solo_datos(payload)
        if isinstance(payload, dict)
        else {"_raw": str(payload)[:2000]}
    )
    row = {
        "id_contrato": id_contrato_de(data),
        "origen": origen,
        "motivo": (motivo or "invalido")[:2000],
        "payload": data,
        "resuelto": False,
    }
    try:
        client.table("ingesta_rechazados").insert(row).execute()
    except Exception as error:
        print(
            f"  [rechazo] no persistido ({error}). El registro NO entra a contratos. "
            "Si falta la tabla, ejecuta ingesta_rechazados.sql",
            flush=True,
        )


def get_max_id(client) -> int:
    try:
        response = (
            client.table("contratos").select("id")
            .order("id", desc=True).limit(1).execute()
        )
        if response.data:
            return int(response.data[0]["id"])
    except Exception as error:
        print(f"  [supabase] no se pudo obtener MAX(id): {error}")
    return 0


def upsert_lote(
    client,
    lote: list[dict],
    reintentos: int = 3,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    for attempt in range(reintentos):
        try:
            client.table("contratos").upsert(lote, on_conflict="id").execute()
            return
        except Exception as error:
            if attempt >= reintentos - 1:
                raise
            wait = 2 ** (attempt + 1)
            print(f"  [retry {attempt + 1}/{reintentos - 1}] {error} — espero {wait}s")
            sleep(wait)


def upsert_supabase(client, filas: list[dict], *, batch_size: int = BATCH_SIZE) -> int:
    total = len(filas)
    batches = -(-total // batch_size)
    errors = 0
    print(f"[supabase] UPSERT {total:,} registros en {batches} lotes de {batch_size}...")
    started = time.time()
    for offset in range(0, total, batch_size):
        batch = filas[offset:offset + batch_size]
        number = offset // batch_size + 1
        try:
            upsert_lote(client, batch)
            elapsed = time.time() - started
            eta = (batches - number) * (elapsed / number)
            print(
                f"  lote {number}/{batches} ({len(batch)} filas) OK "
                f"[{elapsed:.0f}s ~{eta:.0f}s restantes]"
            )
        except Exception as error:
            print(f"  lote {number}/{batches} ERROR: {error}")
            errors += 1
    print(
        f"[supabase] completado en {time.time() - started:.0f}s — "
        f"errores: {errors}/{batches} lotes"
    )
    return errors


def upsert_contratos_pg(dsn: str, filas: list[dict]) -> None:
    columns = ", ".join(COLS_CONTRATOS)
    placeholders = ", ".join(["%s"] * len(COLS_CONTRATOS))
    updates = ", ".join(
        f"{column} = EXCLUDED.{column}" for column in COLS_CONTRATOS if column != "id"
    )
    sql = (
        f"INSERT INTO contratos ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT (id) DO UPDATE SET {updates}"
    )
    params = [[row.get(column) for column in COLS_CONTRATOS] for row in filas]
    with connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(sql, params)
