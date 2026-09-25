"""Preparación determinista de textos, vectores y estadísticas de embeddings.

No realiza llamadas a proveedores ni lecturas/escrituras remotas. El entrypoint
productivo reexporta estos nombres para conservar compatibilidad.
"""
from __future__ import annotations

from seace_monitor.rag.chunking import cuerpo_chunk


MAX_CHARS_GEMINI = 8_000

EMBED_STATS: dict[str, int] = {
    "requests": 0,
    "texts": 0,
    "chars": 0,
    "tokens_api": 0,
}


def reset_embed_stats() -> None:
    for key in EMBED_STATS:
        EMBED_STATS[key] = 0


def print_embed_stats(prefix: str = "") -> None:
    estimated_tokens = EMBED_STATS["chars"] / 4.0
    api_tokens = EMBED_STATS["tokens_api"]
    print(
        f"{prefix}embed_stats requests={EMBED_STATS['requests']} "
        f"texts={EMBED_STATS['texts']} chars={EMBED_STATS['chars']} "
        f"tokens_est(chars/4)={estimated_tokens:.0f} "
        f"tokens_api={api_tokens or '—'}",
        flush=True,
    )


def modo_embed_fila(row: dict, mode: str) -> str:
    if mode in ("header", "body"):
        return mode
    return "body" if (row.get("fuente") or "") == "pdf" else "header"


def texto_para_embed(row: dict, mode: str) -> str:
    if (row.get("fuente") or "") == "pdf":
        prepared = (row.get("chunk_embed_text") or "").strip()
        if prepared:
            return prepared[:MAX_CHARS_GEMINI]

    text = (row.get("texto") or "")[:MAX_CHARS_GEMINI]
    if modo_embed_fila(row, mode) == "body":
        text = cuerpo_chunk(text)[:MAX_CHARS_GEMINI]
    return text


def vec_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in vector) + "]"
