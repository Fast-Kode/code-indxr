from __future__ import annotations

import asyncio
import time
from collections import Counter
from pathlib import Path
from typing import Any, Protocol

from code_indxr.chunker import chunk_file
from code_indxr.config import Settings
from code_indxr.models import CodeChunk, IndexStats, RepoRecord, RepoStatus, SearchResult
from code_indxr.scanner import compute_file_hash, scan_repository
from code_indxr.store import IndexStore


class EmbedderProtocol(Protocol):
    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_query(self, query: str) -> list[float]: ...
    async def health_check(self) -> dict[str, Any]: ...
    async def close(self) -> None: ...


class CodeIndexer:
    """Coordinates fast incremental repository indexing and cross-repo hybrid search."""

    def __init__(
        self,
        settings: Settings,
        store: IndexStore,
        embedder: EmbedderProtocol,
    ) -> None:
        self.settings = settings
        self.store = store
        self.embedder = embedder
        self._repo_locks: dict[str, asyncio.Lock] = {}

    def _get_repo_lock(self, repo_id: str) -> asyncio.Lock:
        lock = self._repo_locks.get(repo_id)
        if lock is None:
            lock = asyncio.Lock()
            self._repo_locks[repo_id] = lock
        return lock

    async def index_project(
        self,
        path: str | Path,
        *,
        name: str | None = None,
        force: bool = False,
    ) -> IndexStats:
        """Scan, incrementally diff, AST-chunk, embed, and persist a project repository."""
        started = time.perf_counter()
        root_path = Path(path).expanduser().resolve()
        if not root_path.is_dir():
            raise FileNotFoundError(f"Project directory not found: {root_path}")

        repo_id = self.store.resolve_repo_id(root_path, custom_name=name)
        repo_name = name.strip() if name and name.strip() else root_path.name

        async with self._get_repo_lock(repo_id):
            scanned_files = await asyncio.to_thread(
                scan_repository,
                root_path,
                self.settings.max_file_bytes,
            )
            existing_manifest = self.store.get_file_manifest(repo_id)

            # Ensure repo row exists before inserting foreign-keyed file manifest entries
            if self.store.get_repo(repo_id) is None:
                self.store.upsert_repo_record(
                    repo_id=repo_id,
                    name=repo_name,
                    root_path=str(root_path),
                    file_count=0,
                    chunk_count=0,
                    languages={},
                )

            scanned_by_rel = {f.rel_path: f for f in scanned_files}
            deleted_paths = sorted(set(existing_manifest.keys()) - set(scanned_by_rel.keys()))

            files_to_embed = []
            touched_manifest_records: list[dict[str, Any]] = []
            files_skipped = 0
            errors: list[str] = []

            for sf in scanned_files:
                prev = existing_manifest.get(sf.rel_path)
                if not force and prev is not None:
                    if prev["mtime_ns"] == sf.mtime_ns and prev["size_bytes"] == sf.size_bytes:
                        files_skipped += 1
                        continue
                    try:
                        current_hash = await asyncio.to_thread(compute_file_hash, sf.abs_path)
                    except OSError as exc:
                        errors.append(f"{sf.rel_path}: {exc}")
                        continue
                    if current_hash == prev["content_hash"]:
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
                    files_to_embed.append((sf, current_hash))
                else:
                    try:
                        current_hash = await asyncio.to_thread(compute_file_hash, sf.abs_path)
                    except OSError as exc:
                        errors.append(f"{sf.rel_path}: {exc}")
                        continue
                    files_to_embed.append((sf, current_hash))

            if deleted_paths:
                self.store.delete_files(repo_id, deleted_paths)

            if touched_manifest_records:
                self.store.upsert_file_manifest(repo_id, touched_manifest_records)

            all_chunks: list[CodeChunk] = []
            replaced_paths: list[str] = []
            new_manifest_records: list[dict[str, Any]] = []

            for sf, content_hash in files_to_embed:
                try:
                    text = await asyncio.to_thread(sf.abs_path.read_text, "utf-8", "ignore")
                except OSError as exc:
                    errors.append(f"{sf.rel_path}: {exc}")
                    continue

                file_chunks = chunk_file(
                    repo_id=repo_id,
                    rel_path=sf.rel_path,
                    content=text,
                    language=sf.language,
                    max_chunk_chars=self.settings.max_chunk_chars,
                    overlap_lines=self.settings.chunk_overlap_lines,
                    min_chunk_chars=self.settings.min_chunk_chars,
                )
                replaced_paths.append(sf.rel_path)
                all_chunks.extend(file_chunks)
                new_manifest_records.append(
                    {
                        "rel_path": sf.rel_path,
                        "mtime_ns": sf.mtime_ns,
                        "size_bytes": sf.size_bytes,
                        "content_hash": content_hash,
                        "language": sf.language,
                        "chunk_count": len(file_chunks),
                    }
                )

            if all_chunks:
                embed_texts = [c.embed_text() for c in all_chunks]
                vectors = await self.embedder.embed_documents(embed_texts)
                self.store.upsert_chunks(
                    repo_id=repo_id,
                    replaced_rel_paths=replaced_paths,
                    chunks=all_chunks,
                    vectors=vectors,
                )
            elif replaced_paths:
                # Files became empty/whitespace-only; clear any prior chunks
                self.store.upsert_chunks(
                    repo_id=repo_id,
                    replaced_rel_paths=replaced_paths,
                    chunks=[],
                    vectors=[],
                )

            if new_manifest_records:
                self.store.upsert_file_manifest(repo_id, new_manifest_records)

            if deleted_paths or replaced_paths:
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

            elapsed = round(time.perf_counter() - started, 3)
            return IndexStats(
                repo_id=repo_id,
                root_path=str(root_path),
                files_scanned=len(scanned_files),
                files_indexed=len(new_manifest_records),
                files_skipped=files_skipped,
                files_removed=len(deleted_paths),
                chunks_indexed=len(all_chunks),
                elapsed_seconds=elapsed,
                errors=errors,
            )

    def _resolve_repo_filters(self, repos: list[str] | None) -> list[str] | None:
        if not repos:
            return None
        resolved_ids: list[str] = []
        for item in repos:
            record = self.store.get_repo(item)
            resolved_ids.append(record.repo_id if record is not None else item)
        return resolved_ids

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
        if mode not in {"hybrid", "vector", "keyword"}:
            raise ValueError(f"Unsupported search mode '{mode}'. Use 'hybrid', 'vector', or 'keyword'.")
        if not query or not query.strip():
            return []

        repo_ids = self._resolve_repo_filters(repos)
        query_vector: list[float] | None = None
        if mode in {"hybrid", "vector"}:
            query_vector = await self.embedder.embed_query(query)

        return await asyncio.to_thread(
            self.store.search,
            query_text=query,
            query_vector=query_vector,
            mode=mode,
            repo_ids=repo_ids,
            languages=languages,
            path_prefix=path_prefix,
            top_k=max(1, min(top_k, 50)),
        )

    def list_projects(self) -> list[RepoRecord]:
        return self.store.list_repos()

    def get_project_status(self, repo_or_path: str) -> RepoStatus:
        record = self.store.get_repo(repo_or_path)
        if record is None:
            return RepoStatus(
                indexed=False,
                repo_id=None,
                root_path=str(Path(repo_or_path).expanduser().resolve()) if Path(repo_or_path).exists() else repo_or_path,
                file_count=0,
                chunk_count=0,
                indexed_at=None,
                languages={},
                stale_files=0,
                new_files=0,
                deleted_files=0,
            )

        manifest = self.store.get_file_manifest(record.repo_id)
        root = Path(record.root_path)
        stale = 0
        new_count = 0
        deleted = 0

        if root.is_dir():
            scanned = scan_repository(root, self.settings.max_file_bytes)
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
        )

    def remove_project(self, repo_or_path: str) -> bool:
        record = self.store.get_repo(repo_or_path)
        target_id = record.repo_id if record is not None else repo_or_path
        return self.store.delete_repo(target_id)

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
