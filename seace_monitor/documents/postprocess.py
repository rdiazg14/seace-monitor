"""Composicion de chunking y embeddings despues de persistir un TDR."""
from __future__ import annotations

from seace_monitor.embeddings.service import run_gemini
from seace_monitor.rag.service import run_solo_pdf


def rechunk_embed_pdf(
    supa,
    contrato_id: int,
    *,
    api_key: str,
    solicitar=None,
    modelo: str | None = None,
    precio_in: float | None = None,
    version_config: int = 0,
) -> None:
    """Regenera unicamente la fuente PDF y sus embeddings para un contrato."""
    run_solo_pdf(supa, [contrato_id], 0)
    kwargs: dict = {}
    if solicitar is not None:
        kwargs["solicitar"] = solicitar
    if modelo is not None:
        kwargs["modelo"] = modelo
    if precio_in is not None:
        kwargs["precio_in"] = precio_in
    if version_config:
        kwargs["version_config"] = version_config
    run_gemini(
        supa,
        0,
        fuente="pdf",
        ids=[contrato_id],
        embed_mode="auto",
        api_key=api_key,
        **kwargs,
    )
