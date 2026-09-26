"""Composicion de chunking y embeddings despues de persistir un TDR."""
from __future__ import annotations

from seace_monitor.embeddings.service import run_gemini
from seace_monitor.rag.service import run_solo_pdf


def rechunk_embed_pdf(supa, contrato_id: int, *, api_key: str) -> None:
    """Regenera unicamente la fuente PDF y sus embeddings para un contrato."""
    run_solo_pdf(supa, [contrato_id], 0)
    run_gemini(
        supa,
        0,
        fuente="pdf",
        ids=[contrato_id],
        embed_mode="auto",
        api_key=api_key,
    )
