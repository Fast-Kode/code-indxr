from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from code_indxr.chunker import extract_file_artifacts
from code_indxr.config import Settings
from code_indxr.embedder import EmbedderError
from code_indxr.locks import RepoLock
from code_indxr.models import (
    CodeChunk,
    FileOutline,
    IndexStats,
    RepoOverview,
    RepoRecord,
    RepoStatus,
    SearchResult,
    SymbolInfo,
)
from code_indxr.scanner import (
    compute_file_hash,
    is_binary_file,
    scan_repository_with_manifests,
)
from code_indxr.store import IndexStore

logger = logging.getLogger("code_indxr.indexer")

LANGUAGE_ALIASES: dict[str, str] = {
    "ts": "typescript",
    "py": "python",
    "js": "javascript",
    "kt": "kotlin",
    "kts": "kotlin",
    "md": "markdown",
    "cs": "csharp",
    "rb": "ruby",
    "rs": "rust",
    "sh": "bash",
    "yml": "yaml",
}


class EmbedderProtocol(Protocol):
    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_query(self, query: str) -> list[float]: ...
    async def health_check(self) -> dict[str, Any]: ...
    async def close(self) -> None: ...


class CodeIndexer:
    """Coordinates batch-resumable repository indexing, outlines, and cross-repo hybrid search."""

    def __init__(
        self,
        settings: Settings,
        store: IndexStore,
        embedder: EmbedderProtocol,
    ) -> None:
        self.settings = settings
        self.store = store
        self.embedder = embedder

    @staticmethod
    def _normalize_languages(languages: list[str] | None) -> list[str] | None:
        if not languages:
            return None
        return [LANGUAGE_ALIASES.get(lang.strip().lower(), lang.strip().lower()) for lang in languages if lang.strip()]

    async def index_repo(
        self,
        path: str | Path,
        *,
        name: str | None = None,
        force: bool = False,
        batch_files: int | None = None,
        time_budget_seconds: float | None = None,
        progress_cb: Callable[[int, int, str], Awaitable[None]] | None = None,
    ) -> IndexStats:
        """Scan, incrementally diff, AST-chunk, embed, and checkpoint-persist a repository."""
        started = time.perf_counter()
        root_path = Path(path).expanduser().resolve()
        if not root_path.is_dir():
            raise FileNotFoundError(f"Repository directory not found: {root_path}")

        repo_id = self.store.resolve_repo_id(root_path, custom_name=name)
        repo_name = name.strip() if name and name.strip() else root_path.name
        effective_batch = max(1, batch_files or self.settings.batch_files)
        budget = 0.0 if time_budget_seconds is None else max(0.0, time_budget_seconds)

        with RepoLock(self.settings.data_dir, repo_id, timeout_seconds=2.0):
            scan_result = await asyncio.to_thread(
                scan_repository_with_manifests,
                root_path,
                self.settings.max_file_bytes,
                sniff_binary=False,
            )
            scanned_files = scan_result.files
            existing_manifest = self.store.get_file_manifest(repo_id)

            if self.store.get_repo(repo_id) is None:
                self.store.upsert_repo_record(
                    repo_id=repo_id,
                    name=repo_name,
                    root_path=str(root_path),
                    file_count=0,
                    chunk_count=0,
                    languages={},
                )

            self.store.replace_repo_manifests(repo_id, scan_result.manifests)

            scanned_by_rel = {f.rel_path: f for f in scanned_files}
            deleted_paths = sorted(set(existing_manifest.keys()) - set(scanned_by_rel.keys()))
            if deleted_paths:
                self.store.delete_files(repo_id, deleted_paths)

            files_to_process = []
            touched_manifest_records: list[dict[str, Any]] = []
            files_skipped = 0
            errors: list[str] = []

            for sf in scanned_files:
                prev = existing_manifest.get(sf.rel_path)
                if (
                    not force
                    and prev is not None
                    and prev["mtime_ns"] == sf.mtime_ns
                    and prev["size_bytes"] == sf.size_bytes
                ):
                    files_skipped += 1
                    continue

                try:
                    if await asyncio.to_thread(is_binary_file, sf.abs_path):
                        if prev is not None:
                            self.store.delete_files(repo_id, [sf.rel_path])
                        continue
                    current_hash = await asyncio.to_thread(compute_file_hash, sf.abs_path)
                except OSError as exc:
                    errors.append(f"{sf.rel_path}: {exc}")
                    continue

                if not force and prev is not None and current_hash == prev["content_hash"]:
                    files_skipped += 1
                    touched_manifest_records.append(
                        {
                            "rel_path": sf.rel_path,
                            "mtime_ns": sf.mtime_ns,
                            "size_bytes": sf.size_bytes,
                            "content_hash": current_hash,
                            "language": sf.language,
                            "chunk_count": prev["chunk_count"],
                        }
                    )
                    continue

                files_to_process.append((sf, current_hash))

            if touched_manifest_records:
                self.store.upsert_file_manifest(repo_id, touched_manifest_records)

            total_to_process = len(files_to_process)
            total_files_indexed = 0
            total_chunks_indexed = 0
            ast_fallback_files = 0
            processed_count = 0
            status = "complete"
            resume_hint: str | None = None

            for batch_start in range(0, total_to_process, effective_batch):
                if batch_start > 0 and budget > 0.0 and (time.perf_counter() - started) >= budget:
                    status = "partial"
                    resume_hint = f"Re-run index_repo('{root_path}') or 'code-indxr index \"{root_path}\"' to resume."
                    break

                batch = files_to_process[batch_start : batch_start + effective_batch]
                batch_chunks: list[CodeChunk] = []
                batch_symbols: list[SymbolInfo] = []
                batch_imports: dict[str, list[str]] = {}
                replaced_paths: list[str] = []
                new_manifest_records: list[dict[str, Any]] = []

                for sf, content_hash in batch:
                    try:
                        text = await asyncio.to_thread(sf.abs_path.read_text, "utf-8", "ignore")
                    except OSError as exc:
                        errors.append(f"{sf.rel_path}: {exc}")
                        continue

                    artifacts = await asyncio.to_thread(
                        extract_file_artifacts,
                        repo_id,
                        sf.rel_path,
                        text,
                        sf.language,
                        self.settings.max_chunk_chars,
                        self.settings.chunk_overlap_lines,
                        self.settings.min_chunk_chars,
                    )
                    if artifacts.used_fallback:
                        ast_fallback_files += 1

                    replaced_paths.append(sf.rel_path)
                    batch_chunks.extend(artifacts.chunks)
                    batch_symbols.extend(artifacts.symbols)
                    if artifacts.imports:
                        batch_imports[sf.rel_path] = artifacts.imports

                    new_manifest_records.append(
                        {
                            "rel_path": sf.rel_path,
                            "mtime_ns": sf.mtime_ns,
                            "size_bytes": sf.size_bytes,
                            "content_hash": content_hash,
                            "language": sf.language,
                            "chunk_count": len(artifacts.chunks),
                        }
                    )

                if batch_chunks:
                    embed_texts = [c.embed_text() for c in batch_chunks]
                    vectors = await self.embedder.embed_documents(embed_texts)
                    self.store.upsert_chunks(
                        repo_id=repo_id,
                        replaced_rel_paths=replaced_paths,
                        chunks=batch_chunks,
                        vectors=vectors,
                    )
                elif replaced_paths:
                    self.store.upsert_chunks(
                        repo_id=repo_id,
                        replaced_rel_paths=replaced_paths,
                        chunks=[],
                        vectors=[],
                    )

                if replaced_paths:
                    self.store.upsert_file_outlines(repo_id, replaced_paths, batch_symbols, batch_imports)
                if new_manifest_records:
                    self.store.upsert_file_manifest(repo_id, new_manifest_records)

                total_files_indexed += len(new_manifest_records)
                total_chunks_indexed += len(batch_chunks)
                processed_count += len(batch)

                if progress_cb is not None:
                    await progress_cb(
                        processed_count, total_to_process, f"Indexed {processed_count}/{total_to_process} files"
                    )

            files_remaining = max(0, total_to_process - processed_count)
            if status == "complete" and (deleted_paths or total_files_indexed > 0):
                await asyncio.to_thread(self.store.rebuild_fts_index)

            updated_manifest = self.store.get_file_manifest(repo_id)
            lang_counts = dict(Counter(meta["language"] for meta in updated_manifest.values()))
            total_chunks = sum(meta["chunk_count"] for meta in updated_manifest.values())

            self.store.upsert_repo_record(
                repo_id=repo_id,
                name=repo_name,
                root_path=str(root_path),
                file_count=len(updated_manifest),
                chunk_count=total_chunks,
                languages=lang_counts,
            )

            errors_truncated = max(0, len(errors) - 50)
            elapsed = round(time.perf_counter() - started, 3)
            return IndexStats(
                repo_id=repo_id,
                root_path=str(root_path),
                files_scanned=len(scanned_files),
                files_indexed=total_files_indexed,
                files_skipped=files_skipped,
                files_removed=len(deleted_paths),
                chunks_indexed=total_chunks_indexed,
                elapsed_seconds=elapsed,
                errors=errors[:50],
                status=status,
                files_remaining=files_remaining,
                ast_fallback_files=ast_fallback_files,
                unreadable_files=scan_result.unreadable_files,
                errors_truncated=errors_truncated,
                resume_hint=resume_hint,
            )

    async def index_project(
        self,
        path: str | Path,
        *,
        name: str | None = None,
        force: bool = False,
    ) -> IndexStats:
        """Backward-compatible alias for index_repo."""
        return await self.index_repo(path, name=name, force=force)

    def _resolve_repo_filters(self, repos: list[str] | None) -> tuple[list[str] | None, list[str]]:
        if not repos:
            return None, []
        resolved_ids: list[str] = []
        unknown: list[str] = []
        for item in repos:
            record = self.store.get_repo(item)
            if record is not None:
                resolved_ids.append(record.repo_id)
            else:
                unknown.append(item)
                resolved_ids.append(item)
        return resolved_ids, unknown

    async def search_code_detailed(
        self,
        query: str,
        *,
        repos: list[str] | None = None,
        languages: list[str] | None = None,
        path_prefix: str | None = None,
        top_k: int = 8,
        mode: str = "hybrid",
    ) -> tuple[list[SearchResult], str, list[str], list[str]]:
        """Search code chunks and return (results, effective_mode, degraded_modes, warnings)."""
        if mode not in {"hybrid", "vector", "keyword"}:
            raise ValueError(f"Unsupported search mode '{mode}'. Use 'hybrid', 'vector', or 'keyword'.")
        if not query or not query.strip():
            return [], mode, [], []

        repo_ids, unknown_repos = self._resolve_repo_filters(repos)
        norm_langs = self._normalize_languages(languages)
        degraded_modes: list[str] = []
        warnings: list[str] = []
        effective_mode = mode

        if unknown_repos:
            known = [r.repo_id for r in self.store.list_repos()]
            warnings.append(f"Unknown repo filter(s): {unknown_repos}. Indexed repos: {known}")

        query_vector: list[float] | None = None
        if mode in {"hybrid", "vector"}:
            try:
                query_vector = await self.embedder.embed_query(query)
            except EmbedderError as exc:
                if mode == "hybrid":
                    logger.warning("Embedder unavailable during hybrid search; falling back to keyword: %s", exc)
                    effective_mode = "keyword"
                    degraded_modes.append("ollama_unavailable_keyword_fallback")
                    warnings.append(str(exc))
                else:
                    raise

        results = await asyncio.to_thread(
            self.store.search,
            query_text=query,
            query_vector=query_vector,
            mode=effective_mode,
            repo_ids=repo_ids,
            languages=norm_langs,
            path_prefix=path_prefix,
            top_k=max(1, min(top_k, 50)),
        )
        return results, effective_mode, degraded_modes, warnings

    async def search_code(
        self,
        query: str,
        *,
        repos: list[str] | None = None,
        languages: list[str] | None = None,
        path_prefix: str | None = None,
        top_k: int = 8,
        mode: str = "hybrid",
    ) -> list[SearchResult]:
        results, _eff, _deg, _warn = await self.search_code_detailed(
            query,
            repos=repos,
            languages=languages,
            path_prefix=path_prefix,
            top_k=top_k,
            mode=mode,
        )
        return results

    def get_repo_overview(
        self,
        repo_or_path: str,
        *,
        max_depth: int = 3,
        max_entries: int = 150,
    ) -> RepoOverview:
        record = self.store.get_repo(repo_or_path)
        if record is None:
            known = [r.repo_id for r in self.store.list_repos()]
            raise ValueError(f"Repository '{repo_or_path}' is not indexed. Indexed repos: {known}")
        overview = self.store.get_repo_overview(
            record.repo_id,
            max_depth=max(1, min(max_depth, 8)),
            max_entries=max(10, min(max_entries, 500)),
        )
        if overview is None:
            raise ValueError(f"Repository '{repo_or_path}' overview not found.")
        return overview

    def get_symbol_outline(
        self,
        repo_or_path: str,
        *,
        path_prefix: str | None = None,
        kind: str | None = None,
        max_symbols: int = 150,
    ) -> FileOutline:
        record = self.store.get_repo(repo_or_path)
        if record is None:
            known = [r.repo_id for r in self.store.list_repos()]
            raise ValueError(f"Repository '{repo_or_path}' is not indexed. Indexed repos: {known}")
        return self.store.get_symbol_outline(
            record.repo_id,
            path_prefix=path_prefix,
            kind=kind,
            max_symbols=max(1, min(max_symbols, 500)),
        )

    def list_repos(self) -> list[RepoRecord]:
        return self.store.list_repos()

    def list_projects(self) -> list[RepoRecord]:
        return self.list_repos()

    def get_repo_status(self, repo_or_path: str, *, fast: bool = False) -> RepoStatus:
        meta = self.store.get_store_meta()
        record = self.store.get_repo(repo_or_path)
        if record is None:
            resolved_str = repo_or_path
            try:
                candidate = Path(repo_or_path).expanduser()
                if candidate.exists():
                    resolved_str = str(candidate.resolve())
            except OSError:
                pass
            return RepoStatus(
                indexed=False,
                repo_id=None,
                root_path=resolved_str,
                file_count=0,
                chunk_count=0,
                indexed_at=None,
                languages={},
                stale_files=0,
                new_files=0,
                deleted_files=0,
                schema_version=self.store.schema_version,
                embed_model=meta.get("embed_model"),
                vector_dim=int(meta["vector_dim"]) if meta.get("vector_dim") else None,
                fts_dirty=meta.get("fts_dirty", "0") == "1",
            )

        stale = 0
        new_count = 0
        deleted = 0

        if not fast:
            manifest = self.store.get_file_manifest(record.repo_id)
            root = Path(record.root_path)
            if root.is_dir():
                scanned = scan_repository_with_manifests(root, self.settings.max_file_bytes, sniff_binary=False).files
                scanned_map = {f.rel_path: f for f in scanned}
                deleted = len(set(manifest.keys()) - set(scanned_map.keys()))
                for rel, sf in scanned_map.items():
                    prev = manifest.get(rel)
                    if prev is None:
                        new_count += 1
                    elif prev["mtime_ns"] != sf.mtime_ns or prev["size_bytes"] != sf.size_bytes:
                        stale += 1

        return RepoStatus(
            indexed=True,
            repo_id=record.repo_id,
            root_path=record.root_path,
            file_count=record.file_count,
            chunk_count=record.chunk_count,
            indexed_at=record.indexed_at,
            languages=record.languages,
            stale_files=stale,
            new_files=new_count,
            deleted_files=deleted,
            schema_version=self.store.schema_version,
            embed_model=meta.get("embed_model"),
            vector_dim=int(meta["vector_dim"]) if meta.get("vector_dim") else None,
            fts_dirty=meta.get("fts_dirty", "0") == "1",
        )

    def get_project_status(self, repo_or_path: str) -> RepoStatus:
        return self.get_repo_status(repo_or_path)

    def remove_repo(self, repo_or_path: str) -> bool:
        record = self.store.get_repo(repo_or_path)
        target_id = record.repo_id if record is not None else repo_or_path
        with RepoLock(self.settings.data_dir, target_id, timeout_seconds=2.0):
            return self.store.delete_repo(target_id)

    def remove_project(self, repo_or_path: str) -> bool:
        return self.remove_repo(repo_or_path)

    async def health_check(self) -> dict[str, Any]:
        embedder_status = await self.embedder.health_check()
        store_stats = self.store.total_stats()
        return {
            "ok": bool(embedder_status.get("ok")),
            "embedder": embedder_status,
            "store": store_stats,
        }

    async def close(self) -> None:
        await self.embedder.close()
        self.store.close()
