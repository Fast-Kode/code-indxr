from __future__ import annotations

import logging
import sys

from code_indxr.config import Settings
from code_indxr.embedder import OllamaEmbedder
from code_indxr.indexer import CodeIndexer
from code_indxr.store import IndexStore


def configure_logging(level: str = "WARNING") -> None:
    """Configure stderr-only logging at process entry points (never stdout)."""
    numeric = getattr(logging, level.upper(), logging.WARNING)
    logging.basicConfig(
        level=numeric,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )


def build_indexer(settings: Settings | None = None) -> CodeIndexer:
    """Construct the shared IndexStore, OllamaEmbedder, and CodeIndexer."""
    active_settings = settings or Settings.from_env()
    store = IndexStore(active_settings.data_dir, embed_model=active_settings.embed_model)
    embedder = OllamaEmbedder(
        base_url=active_settings.ollama_url,
        model=active_settings.embed_model,
        batch_size=active_settings.embed_batch_size,
        concurrency=active_settings.embed_concurrency,
        timeout=active_settings.embed_timeout_seconds,
    )
    return CodeIndexer(settings=active_settings, store=store, embedder=embedder)
