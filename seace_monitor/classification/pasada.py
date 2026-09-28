"""Pasadas Gemini P1/P2 sobre lotes de contratos.

`clasificar_lote` conecta el provider con la cuota C4 y el contador de
tokens (deps inyectadas); `correr_pasada` itera los lotes tolerando fallos
por lote — un lote fallido marca sus ids como sin_respuesta, no aborta.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx

from .contracts import RESPONSE_SCHEMA, SYSTEM_PROMPT
from .cuota import (
    CUOTA_C4_PATH,
    MAX_LLAMADAS_DIA_DEFAULT,
    CupoClasificacion,
    acumular_tokens,
    assert_cuota_c4,
    registrar_llamada_c4,
)
from .gemini_provider import clasificar_lote_gemini
from .rules import emparejar_lote, parse_array, parse_p1_item, parse_p2_item
from .service import user_prompt

GEMINI_BACKOFF = (2.0, 4.0, 8.0, 16.0, 32.0, 64.0)
TIMEOUT_S = 120.0


def clasificar_lote(
    client: httpx.Client,
    lote: list[dict],
    *,
    api_key: str,
    url: str,
    supa=None,
    stats: dict,
    system_prompt: str = SYSTEM_PROMPT,
    schema: dict = RESPONSE_SCHEMA,
    armar_prompt: Callable[[list[dict]], str] = user_prompt,
    max_llamadas: int = MAX_LLAMADAS_DIA_DEFAULT,
    cuota_path: Path = CUOTA_C4_PATH,
    backoff: tuple[float, ...] = GEMINI_BACKOFF,
    timeout: float = TIMEOUT_S,
    transporte: Callable[..., list[dict]] | None = None,
    modelo: str | None = None,
    precio: dict | None = None,
    version_config: int = 0,
) -> list[dict]:
    transporte = transporte or clasificar_lote_gemini
    def on_success(body: dict) -> None:
        acumular_tokens(stats, body)
        registrar_llamada_c4(
            supa, body, max_llamadas=max_llamadas, path=cuota_path
        )
        if modelo is not None and supa is not None:
            um = body.get("usageMetadata") or {}
            prompt = int(um.get("promptTokenCount") or 0)
            completion = int(um.get("candidatesTokenCount") or 0)
            tarifas = precio or {}
            try:
                supa.table("uso_ia").insert({
                    "componente": "clasificar", "modelo": modelo,
                    "tokens_prompt": prompt, "tokens_completion": completion,
                    "tokens_total": prompt + completion,
                    "costo_usd": (prompt * float(tarifas.get("in", 0))
                                  + completion * float(tarifas.get("out", 0))) / 1_000_000,
                    "cache_hit": False,
                    "detalle": {"version_config": version_config,
                                "precio": tarifas, "tarifa_estimada": True},
                }).execute()
            except Exception:
                print("  [warn] log_uso_ia clasificación falló", flush=True)

    return transporte(
        client,
        lote,
        system_prompt=system_prompt,
        schema=schema,
        armar_prompt=armar_prompt,
        api_key=api_key,
        url=url,
        parse_response=parse_array,
        before_call=lambda: assert_cuota_c4(
            supa, max_llamadas=max_llamadas, path=cuota_path
        ),
        on_success=on_success,
        quota_error_types=(CupoClasificacion,),
        backoff=backoff,
        timeout=timeout,
    )


def correr_pasada(
    filas: list[dict],
    batch: int,
    *,
    etiqueta: str,
    clasificar: Callable[[list[dict]], list[dict]],
) -> tuple[dict[int, dict], set[int]]:
    matched_out: dict[int, dict] = {}
    sin: set[int] = set()
    n_lotes = -(-len(filas) // batch) if filas else 0
    parse = parse_p1_item if etiqueta == "P1" else parse_p2_item
    for i in range(0, len(filas), batch):
        lote = filas[i: i + batch]
        num = i // batch + 1
        print(f"  lote {etiqueta} {num}/{n_lotes} n={len(lote)}", flush=True)
        try:
            raw = clasificar(lote)
        except Exception as e:
            print(f"  [lote {etiqueta} fallo] {e}", flush=True)
            for row in lote:
                sin.add(int(row["id"]))
            continue
        matched, missing = emparejar_lote(lote, raw)
        sin.update(missing)
        for cid, item in matched.items():
            parsed = parse(item)
            if parsed is None:
                print(
                    f"    [aviso] id={cid} {etiqueta} categoria invalida "
                    f"{item.get('categoria')!r} -> sin_respuesta",
                    flush=True,
                )
                sin.add(cid)
            else:
                matched_out[cid] = parsed
    return matched_out, sin
