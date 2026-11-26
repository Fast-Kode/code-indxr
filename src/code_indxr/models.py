from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


class RepoLockError(RuntimeError):
    """Raised when a repository write lock is already held by another process."""


@dataclass(frozen=True)
class ScannedFile:
    rel_path: str
    abs_path: Path
    size_bytes: int
    mtime_ns: int
    language: str


@dataclass(frozen=True)
class DetectedManifest:
    rel_path: str
    kind: str


@dataclass(frozen=True)
class ScanResult:
    files: list[ScannedFile]
    manifests: list[DetectedManifest]
    unreadable_files: int = 0


@dataclass(frozen=True)
class SymbolInfo:
    rel_path: str
    symbol_name: str
    symbol_kind: str
    signature: str
    docstring_excerpt: str
    start_line: int
    end_line: int

    def to_dict(self, include_docstrings: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "file_path": self.rel_path,
            "symbol": self.symbol_name,
            "kind": self.symbol_kind,
            "signature": self.signature,
            "lines": f"{self.start_line}-{self.end_line}",
            "start_line": self.start_line,
            "end_line": self.end_line,
        }
        if include_docstrings and self.docstring_excerpt:
            payload["docstring"] = self.docstring_excerpt
        return payload


@dataclass(frozen=True)
class FileArtifacts:
    chunks: list[CodeChunk]
    symbols: list[SymbolInfo]
    imports: list[str]
    used_fallback: bool = False


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
    status: str = "complete"
    files_remaining: int = 0
    ast_fallback_files: int = 0
    unreadable_files: int = 0
    errors_truncated: int = 0
    resume_hint: str | None = None

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
    schema_version: int = 2
    embed_model: str | None = None
    vector_dim: int | None = None
    fts_dirty: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RepoOverview:
    repo_id: str
    name: str
    root_path: str
    indexed_at: str
    file_count: int
    chunk_count: int
    languages: dict[str, int]
    manifests: list[str]
    tree_entries: list[str]
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FileOutline:
    repo_id: str
    total_symbols: int
    symbols: list[SymbolInfo]
    imports_by_file: dict[str, list[str]]
    truncated: bool

    def to_dict(self, include_docstrings: bool = False) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "total_symbols": self.total_symbols,
            "count": len(self.symbols),
            "truncated": self.truncated,
            "symbols": [s.to_dict(include_docstrings=include_docstrings) for s in self.symbols],
            "imports_by_file": self.imports_by_file,
        }
