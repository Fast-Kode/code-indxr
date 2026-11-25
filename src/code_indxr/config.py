from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _default_data_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "code-indxr"
    return Path.home() / ".local" / "share" / "code-indxr"


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for code-indxr, overridable via environment variables."""

    data_dir: Path = _default_data_dir()
    ollama_url: str = "http://127.0.0.1:11434"
    embed_model: str = "nomic-embed-text"
    embed_batch_size: int = 32
    embed_concurrency: int = 2
    embed_timeout_seconds: float = 120.0
    max_file_bytes: int = 512 * 1024
    max_chunk_chars: int = 2400
    chunk_overlap_lines: int = 6
    min_chunk_chars: int = 40

    @classmethod
    def from_env(cls) -> Settings:
        raw_dir = os.environ.get("CODE_INDXR_DATA_DIR")
        data_dir = Path(raw_dir).expanduser().resolve() if raw_dir else _default_data_dir()
        return cls(
            data_dir=data_dir,
            ollama_url=os.environ.get("CODE_INDXR_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
            embed_model=os.environ.get("CODE_INDXR_EMBED_MODEL", "nomic-embed-text"),
            embed_batch_size=max(1, int(os.environ.get("CODE_INDXR_BATCH_SIZE", "32"))),
            embed_concurrency=max(1, int(os.environ.get("CODE_INDXR_CONCURRENCY", "2"))),
            embed_timeout_seconds=max(5.0, float(os.environ.get("CODE_INDXR_TIMEOUT", "120"))),
            max_file_bytes=max(1024, int(os.environ.get("CODE_INDXR_MAX_FILE_BYTES", str(512 * 1024)))),
            max_chunk_chars=max(200, int(os.environ.get("CODE_INDXR_MAX_CHUNK_CHARS", "2400"))),
            chunk_overlap_lines=max(0, int(os.environ.get("CODE_INDXR_CHUNK_OVERLAP_LINES", "6"))),
        )
