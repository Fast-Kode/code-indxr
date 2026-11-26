from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _default_data_dir() -> Path:
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "code-indxr"
        return Path.home() / "AppData" / "Local" / "code-indxr"

    if sys.platform == "darwin":
        legacy_dir = Path.home() / ".local" / "share" / "code-indxr"
        mac_dir = Path.home() / "Library" / "Application Support" / "code-indxr"
        if legacy_dir.exists() and not mac_dir.exists():
            return legacy_dir
        return mac_dir

    xdg_data = os.environ.get("XDG_DATA_HOME")
    if xdg_data:
        return Path(xdg_data).expanduser() / "code-indxr"
    return Path.home() / ".local" / "share" / "code-indxr"


def _default_ollama_url() -> str:
    explicit = os.environ.get("CODE_INDXR_OLLAMA_URL")
    if explicit and explicit.strip():
        return explicit.strip().rstrip("/")
    host = os.environ.get("OLLAMA_HOST")
    if host and host.strip():
        normalized = host.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            normalized = f"http://{normalized}"
        normalized = normalized.replace("://0.0.0.0", "://127.0.0.1").replace("://[::]", "://127.0.0.1")
        return normalized
    return "http://127.0.0.1:11434"


def _parse_int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid integer for environment variable {name}: {raw!r}") from exc
    return max(minimum, value)


def _parse_float_env(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid float for environment variable {name}: {raw!r}") from exc
    return max(minimum, value)


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for code-indxr, overridable via environment variables."""

    data_dir: Path = field(default_factory=_default_data_dir)
    ollama_url: str = field(default_factory=_default_ollama_url)
    embed_model: str = "nomic-embed-text"
    embed_batch_size: int = 32
    embed_concurrency: int = 2
    embed_timeout_seconds: float = 120.0
    max_file_bytes: int = 512 * 1024
    max_chunk_chars: int = 2400
    chunk_overlap_lines: int = 6
    min_chunk_chars: int = 40
    batch_files: int = 50
    mcp_time_budget_seconds: float = 45.0
    log_level: str = "WARNING"

    @classmethod
    def from_env(cls) -> Settings:
        raw_dir = os.environ.get("CODE_INDXR_DATA_DIR")
        data_dir = Path(raw_dir).expanduser().resolve() if raw_dir and raw_dir.strip() else _default_data_dir()
        return cls(
            data_dir=data_dir,
            ollama_url=_default_ollama_url(),
            embed_model=os.environ.get("CODE_INDXR_EMBED_MODEL", "nomic-embed-text").strip() or "nomic-embed-text",
            embed_batch_size=_parse_int_env("CODE_INDXR_BATCH_SIZE", 32, minimum=1),
            embed_concurrency=_parse_int_env("CODE_INDXR_CONCURRENCY", 2, minimum=1),
            embed_timeout_seconds=_parse_float_env("CODE_INDXR_TIMEOUT", 120.0, minimum=5.0),
            max_file_bytes=_parse_int_env("CODE_INDXR_MAX_FILE_BYTES", 512 * 1024, minimum=1024),
            max_chunk_chars=_parse_int_env("CODE_INDXR_MAX_CHUNK_CHARS", 2400, minimum=200),
            chunk_overlap_lines=_parse_int_env("CODE_INDXR_CHUNK_OVERLAP_LINES", 6, minimum=0),
            min_chunk_chars=_parse_int_env("CODE_INDXR_MIN_CHUNK_CHARS", 40, minimum=1),
            batch_files=_parse_int_env("CODE_INDXR_BATCH_FILES", 50, minimum=1),
            mcp_time_budget_seconds=_parse_float_env("CODE_INDXR_MCP_TIME_BUDGET", 45.0, minimum=0.0),
            log_level=os.environ.get("CODE_INDXR_LOG_LEVEL", "WARNING").strip().upper() or "WARNING",
        )
