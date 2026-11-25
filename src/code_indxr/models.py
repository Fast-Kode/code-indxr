from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ScannedFile:
    rel_path: str
    abs_path: Path
    size_bytes: int
    mtime_ns: int
    language: str


@dataclass(frozen=True)
class CodeChunk:
    chunk_id: str
    repo_id: str
    rel_path: str
    language: str
    symbol_name: str
    symbol_kind: str
    start_line: int
    end_line: int
    content: str

    def embed_text(self) -> str:
        header = (
            f"// File: {self.rel_path} | Language: {self.language} | "
            f"Symbol: {self.symbol_name} ({self.symbol_kind}) | "
            f"Lines: {self.start_line}-{self.end_line}"
        )
        return f"{header}\n{self.content}"


@dataclass(frozen=True)
class RepoRecord:
    repo_id: str
    name: str
    root_path: str
    indexed_at: str
    file_count: int
    chunk_count: int
    languages: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IndexStats:
    repo_id: str
    root_path: str
    files_scanned: int
    files_indexed: int
    files_skipped: int
    files_removed: int
    chunks_indexed: int
    elapsed_seconds: float
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SearchResult:
    chunk_id: str
    repo_id: str
    rel_path: str
    abs_path: str
    language: str
    symbol_name: str
    symbol_kind: str
    start_line: int
    end_line: int
    score: float
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "file_path": self.rel_path,
            "abs_path": self.abs_path,
            "language": self.language,
            "symbol": self.symbol_name,
            "symbol_kind": self.symbol_kind,
            "lines": f"{self.start_line}-{self.end_line}",
            "start_line": self.start_line,
            "end_line": self.end_line,
            "score": round(self.score, 4),
            "content": self.content,
        }


@dataclass(frozen=True)
class RepoStatus:
    indexed: bool
    repo_id: str | None
    root_path: str
    file_count: int
    chunk_count: int
    indexed_at: str | None
    languages: dict[str, int]
    stale_files: int
    new_files: int
    deleted_files: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
