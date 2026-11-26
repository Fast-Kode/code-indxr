from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import lancedb
import pyarrow as pa
from lancedb.index import FTS

from code_indxr.models import (
    CodeChunk,
    DetectedManifest,
    FileOutline,
    RepoOverview,
    RepoRecord,
    SearchResult,
    SymbolInfo,
)

logger = logging.getLogger("code_indxr.store")

CHUNKS_TABLE_NAME = "code_chunks"
SCHEMA_VERSION = 2


class StoreCompatibilityError(RuntimeError):
    """Raised when schema version or embedding model/dimension is incompatible with existing data."""


def _escape_sql(value: str) -> str:
    return value.replace("'", "''")


def _escape_like_prefix(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip().lower()).strip("-")
    return slug or "repo"


class IndexStore:
    """Thread-safe SQLite manifest + LanceDB vector & full-text hybrid store."""

    def __init__(self, data_dir: Path, embed_model: str | None = None) -> None:
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

        self._db_path = self.data_dir / "manifest.db"
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA busy_timeout=10000;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")

        self._lance_dir = self.data_dir / "lancedb"
        self._lance_dir.mkdir(parents=True, exist_ok=True)
        self._lance = lancedb.connect(str(self._lance_dir))
        self._cached_generation: int | None = None
        self._cached_table: Any | None = None

        self.schema_version = self._init_and_migrate(embed_model=embed_model)
        if embed_model:
            self._validate_embed_model(embed_model)

    def _init_and_migrate(self, embed_model: str | None) -> int:
        with self._lock:
            current_version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if current_version > SCHEMA_VERSION:
                raise StoreCompatibilityError(
                    f"Database schema version v{current_version} is newer than supported v{SCHEMA_VERSION}. "
                    f"Upgrade code-indxr before accessing {self._db_path}."
                )

            existing_repos_table = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='repos'"
            ).fetchone()

            if current_version == 0 and existing_repos_table is None:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._create_v2_schema()
                    if embed_model:
                        self._set_meta_unlocked("embed_model", embed_model)
                    self._set_meta_unlocked("fts_dirty", "0")
                    self._set_meta_unlocked("store_generation", "1")
                    self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise
                return SCHEMA_VERSION

            if current_version < 2:
                backup_path = self.data_dir / "manifest.db.bak.v1"
                if not backup_path.exists():
                    self._conn.commit()
                    shutil.copy2(self._db_path, backup_path)

                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._create_v2_schema()
                    detected_dim = self._inspect_existing_vector_dim()
                    if detected_dim is not None:
                        self._set_meta_unlocked("vector_dim", str(detected_dim))
                    if embed_model:
                        self._set_meta_unlocked("embed_model", embed_model)
                    self._set_meta_unlocked("fts_dirty", "0")
                    self._set_meta_unlocked("store_generation", "1")
                    self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise
                return SCHEMA_VERSION

            return current_version

    def _create_v2_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS repos (
                repo_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                root_path TEXT NOT NULL UNIQUE,
                indexed_at TEXT NOT NULL,
                file_count INTEGER NOT NULL DEFAULT 0,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                languages_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS files (
                repo_id TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                mtime_ns INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                language TEXT NOT NULL,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (repo_id, rel_path),
                FOREIGN KEY (repo_id) REFERENCES repos(repo_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS store_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS repo_manifests (
                repo_id TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                manifest_kind TEXT NOT NULL,
                PRIMARY KEY (repo_id, rel_path),
                FOREIGN KEY (repo_id) REFERENCES repos(repo_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS file_symbols (
                repo_id TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                symbol_name TEXT NOT NULL,
                symbol_kind TEXT NOT NULL,
                signature TEXT NOT NULL,
                docstring_excerpt TEXT NOT NULL DEFAULT '',
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                FOREIGN KEY (repo_id) REFERENCES repos(repo_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_file_symbols_repo_path
                ON file_symbols(repo_id, rel_path);

            CREATE TABLE IF NOT EXISTS file_imports (
                repo_id TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                import_text TEXT NOT NULL,
                FOREIGN KEY (repo_id) REFERENCES repos(repo_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_file_imports_repo_path
                ON file_imports(repo_id, rel_path);
            """
        )

    def _inspect_existing_vector_dim(self) -> int | None:
        table = self._open_chunks_table(force_refresh=True)
        if table is None:
            return None
        try:
            field = table.schema.field("vector")
            return getattr(field.type, "list_size", None)
        except Exception:
            return None

    def _set_meta_unlocked(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO store_meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def get_store_meta(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM store_meta").fetchall()
            return {str(r["key"]): str(r["value"]) for r in rows}

    def _validate_embed_model(self, embed_model: str) -> None:
        with self._lock:
            meta = self.get_store_meta()
            stored_model = meta.get("embed_model")
            has_chunks = self._open_chunks_table() is not None
            if stored_model and stored_model != embed_model and (has_chunks or meta.get("vector_dim")):
                raise StoreCompatibilityError(
                    f"Store at {self.data_dir} was indexed with embedding model '{stored_model}', "
                    f"but '{embed_model}' is configured. Delete the store or set a separate CODE_INDXR_DATA_DIR."
                )
            if not stored_model:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._set_meta_unlocked("embed_model", embed_model)
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise

    def _bump_generation_unlocked(self, fts_dirty: bool | None = None) -> None:
        row = self._conn.execute("SELECT value FROM store_meta WHERE key = 'store_generation'").fetchone()
        gen = int(row["value"]) + 1 if row is not None else 1
        self._set_meta_unlocked("store_generation", str(gen))
        if fts_dirty is not None:
            self._set_meta_unlocked("fts_dirty", "1" if fts_dirty else "0")

    def _open_chunks_table(self, force_refresh: bool = False) -> Any | None:
        gen_str = self.get_store_meta().get("store_generation", "0")
        gen = int(gen_str)
        if not force_refresh and self._cached_table is not None and self._cached_generation == gen:
            return self._cached_table

        listed = self._lance.list_tables()
        names = getattr(listed, "tables", listed)
        if CHUNKS_TABLE_NAME not in names:
            self._cached_table = None
            self._cached_generation = gen
            return None

        table = self._lance.open_table(CHUNKS_TABLE_NAME)
        self._cached_table = table
        self._cached_generation = gen
        return table

    def _retry_lance(self, fn: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                return fn()
            except Exception as exc:
                last_exc = exc
                if attempt < 2 and "commit" in str(exc).lower():
                    time.sleep(0.05 * (attempt + 1))
                    continue
                raise
        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _path_where_sql() -> str:
        return "LOWER(root_path) = LOWER(?)" if os.name == "nt" else "root_path = ?"

    def resolve_repo_id(self, root_path: Path, custom_name: str | None = None) -> str:
        """Return existing repo_id for root_path, or allocate a deterministic slug."""
        normalized_root = str(root_path.resolve())
        with self._lock:
            row = self._conn.execute(
                f"SELECT repo_id FROM repos WHERE {self._path_where_sql()}",
                (normalized_root,),
            ).fetchone()
            if row is not None:
                return str(row["repo_id"])

            base_slug = _slugify(custom_name or root_path.resolve().name)
            collision = self._conn.execute(
                "SELECT root_path FROM repos WHERE repo_id = ?",
                (base_slug,),
            ).fetchone()
            if collision is None:
                return base_slug

            key_str = normalized_root.lower() if os.name == "nt" else normalized_root
            suffix = hashlib.blake2b(key_str.encode("utf-8"), digest_size=3).hexdigest()
            return f"{base_slug}-{suffix}"

    def get_repo(self, repo_or_path: str) -> RepoRecord | None:
        candidate = repo_or_path.strip()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM repos WHERE repo_id = ?",
                (candidate,),
            ).fetchone()
            if row is None:
                try:
                    resolved = str(Path(candidate).expanduser().resolve())
                except OSError:
                    resolved = candidate
                row = self._conn.execute(
                    f"SELECT * FROM repos WHERE {self._path_where_sql()}",
                    (resolved,),
                ).fetchone()
            if row is None:
                return None
            return self._row_to_repo(row)

    def list_repos(self) -> list[RepoRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM repos ORDER BY indexed_at DESC").fetchall()
            return [self._row_to_repo(row) for row in rows]

    @staticmethod
    def _row_to_repo(row: sqlite3.Row) -> RepoRecord:
        return RepoRecord(
            repo_id=row["repo_id"],
            name=row["name"],
            root_path=row["root_path"],
            indexed_at=row["indexed_at"],
            file_count=int(row["file_count"]),
            chunk_count=int(row["chunk_count"]),
            languages=json.loads(row["languages_json"] or "{}"),
        )

    def upsert_repo_record(
        self,
        *,
        repo_id: str,
        name: str,
        root_path: str,
        file_count: int,
        chunk_count: int,
        languages: dict[str, int],
    ) -> None:
        now_iso = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    INSERT INTO repos (repo_id, name, root_path, indexed_at, file_count, chunk_count, languages_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(repo_id) DO UPDATE SET
                        name = excluded.name,
                        root_path = excluded.root_path,
                        indexed_at = excluded.indexed_at,
                        file_count = excluded.file_count,
                        chunk_count = excluded.chunk_count,
                        languages_json = excluded.languages_json
                    """,
                    (repo_id, name, root_path, now_iso, file_count, chunk_count, json.dumps(languages)),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def get_file_manifest(self, repo_id: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT rel_path, mtime_ns, size_bytes, content_hash, language, chunk_count FROM files WHERE repo_id = ?",
                (repo_id,),
            ).fetchall()
            return {
                row["rel_path"]: {
                    "mtime_ns": int(row["mtime_ns"]),
                    "size_bytes": int(row["size_bytes"]),
                    "content_hash": row["content_hash"],
                    "language": row["language"],
                    "chunk_count": int(row["chunk_count"]),
                }
                for row in rows
            }

    def upsert_file_manifest(self, repo_id: str, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.executemany(
                    """
                    INSERT INTO files (repo_id, rel_path, mtime_ns, size_bytes, content_hash, language, chunk_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(repo_id, rel_path) DO UPDATE SET
                        mtime_ns = excluded.mtime_ns,
                        size_bytes = excluded.size_bytes,
                        content_hash = excluded.content_hash,
                        language = excluded.language,
                        chunk_count = excluded.chunk_count
                    """,
                    [
                        (
                            repo_id,
                            r["rel_path"],
                            r["mtime_ns"],
                            r["size_bytes"],
                            r["content_hash"],
                            r["language"],
                            r["chunk_count"],
                        )
                        for r in records
                    ],
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def replace_repo_manifests(self, repo_id: str, manifests: list[DetectedManifest]) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM repo_manifests WHERE repo_id = ?", (repo_id,))
                if manifests:
                    self._conn.executemany(
                        "INSERT INTO repo_manifests (repo_id, rel_path, manifest_kind) VALUES (?, ?, ?)",
                        [(repo_id, m.rel_path, m.kind) for m in manifests],
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def upsert_file_outlines(
        self,
        repo_id: str,
        rel_paths: list[str],
        symbols: list[SymbolInfo],
        imports_by_file: dict[str, list[str]],
    ) -> None:
        if not rel_paths:
            return
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.executemany(
                    "DELETE FROM file_symbols WHERE repo_id = ? AND rel_path = ?",
                    [(repo_id, p) for p in rel_paths],
                )
                self._conn.executemany(
                    "DELETE FROM file_imports WHERE repo_id = ? AND rel_path = ?",
                    [(repo_id, p) for p in rel_paths],
                )
                if symbols:
                    self._conn.executemany(
                        """
                        INSERT INTO file_symbols (
                            repo_id, rel_path, symbol_name, symbol_kind,
                            signature, docstring_excerpt, start_line, end_line
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                repo_id,
                                s.rel_path,
                                s.symbol_name,
                                s.symbol_kind,
                                s.signature,
                                s.docstring_excerpt,
                                s.start_line,
                                s.end_line,
                            )
                            for s in symbols
                        ],
                    )
                import_rows = [(repo_id, rel_path, imp) for rel_path, imps in imports_by_file.items() for imp in imps]
                if import_rows:
                    self._conn.executemany(
                        "INSERT INTO file_imports (repo_id, rel_path, import_text) VALUES (?, ?, ?)",
                        import_rows,
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _delete_chunks_for_paths(self, table: Any, repo_id: str, rel_paths: list[str]) -> None:
        if not rel_paths:
            return
        safe_repo = _escape_sql(repo_id)
        batch_size = 100
        for i in range(0, len(rel_paths), batch_size):
            batch = rel_paths[i : i + batch_size]
            quoted = ", ".join(f"'{_escape_sql(p)}'" for p in batch)
            self._retry_lance(lambda q=quoted: table.delete(f"repo_id = '{safe_repo}' AND rel_path IN ({q})"))

    def upsert_chunks(
        self,
        *,
        repo_id: str,
        replaced_rel_paths: list[str],
        chunks: list[CodeChunk],
        vectors: list[list[float]],
    ) -> None:
        """Replace chunks for modified files in LanceDB using batched PyArrow inserts."""
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors must have the same length")

        table = self._open_chunks_table()
        if table is not None and replaced_rel_paths:
            self._delete_chunks_for_paths(table, repo_id, replaced_rel_paths)

        if not chunks:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._bump_generation_unlocked(fts_dirty=True)
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise
            return

        dim = len(vectors[0])
        meta = self.get_store_meta()
        stored_dim = meta.get("vector_dim")
        if stored_dim and int(stored_dim) != dim:
            raise StoreCompatibilityError(
                f"Vector dimension mismatch: store expects {stored_dim}, but embedder returned {dim}. "
                f"Delete the store or set a separate CODE_INDXR_DATA_DIR."
            )

        schema = pa.schema(
            [
                pa.field("chunk_id", pa.string()),
                pa.field("repo_id", pa.string()),
                pa.field("rel_path", pa.string()),
                pa.field("language", pa.string()),
                pa.field("symbol_name", pa.string()),
                pa.field("symbol_kind", pa.string()),
                pa.field("start_line", pa.int32()),
                pa.field("end_line", pa.int32()),
                pa.field("content", pa.string()),
                pa.field("search_text", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), dim)),
            ]
        )

        rows = [
            {
                "chunk_id": chunk.chunk_id,
                "repo_id": chunk.repo_id,
                "rel_path": chunk.rel_path,
                "language": chunk.language,
                "symbol_name": chunk.symbol_name,
                "symbol_kind": chunk.symbol_kind,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "content": chunk.content,
                "search_text": f"{chunk.rel_path} {chunk.symbol_name} {chunk.content}",
                "vector": vector,
            }
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]

        arrow_table = pa.Table.from_pylist(rows, schema=schema)
        if table is None:
            self._retry_lance(lambda: self._lance.create_table(CHUNKS_TABLE_NAME, data=arrow_table))
        else:
            self._retry_lance(lambda: table.add(arrow_table))

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if not stored_dim:
                    self._set_meta_unlocked("vector_dim", str(dim))
                self._bump_generation_unlocked(fts_dirty=True)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def delete_files(self, repo_id: str, rel_paths: list[str]) -> None:
        if not rel_paths:
            return
        table = self._open_chunks_table()
        if table is not None:
            self._delete_chunks_for_paths(table, repo_id, rel_paths)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.executemany(
                    "DELETE FROM files WHERE repo_id = ? AND rel_path = ?",
                    [(repo_id, p) for p in rel_paths],
                )
                self._conn.executemany(
                    "DELETE FROM file_symbols WHERE repo_id = ? AND rel_path = ?",
                    [(repo_id, p) for p in rel_paths],
                )
                self._conn.executemany(
                    "DELETE FROM file_imports WHERE repo_id = ? AND rel_path = ?",
                    [(repo_id, p) for p in rel_paths],
                )
                self._bump_generation_unlocked(fts_dirty=True)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def delete_repo(self, repo_id: str) -> bool:
        with self._lock:
            existing = self._conn.execute("SELECT 1 FROM repos WHERE repo_id = ?", (repo_id,)).fetchone()
            if existing is None:
                return False

        table = self._open_chunks_table()
        if table is not None:
            self._retry_lance(lambda: table.delete(f"repo_id = '{_escape_sql(repo_id)}'"))

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute("DELETE FROM repos WHERE repo_id = ?", (repo_id,))
                self._bump_generation_unlocked(fts_dirty=True)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return True

    def rebuild_fts_index(self) -> None:
        """Create or refresh native LanceDB full-text index on search_text."""
        table = self._open_chunks_table(force_refresh=True)
        if table is None or table.count_rows() == 0:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    self._set_meta_unlocked("fts_dirty", "0")
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise
            return

        self._retry_lance(lambda: table.create_index("search_text", config=FTS(), replace=True))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._bump_generation_unlocked(fts_dirty=False)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _build_where_clause(
        self,
        repo_ids: list[str] | None,
        languages: list[str] | None,
        path_prefix: str | None,
    ) -> str | None:
        clauses: list[str] = []
        if repo_ids:
            quoted = ", ".join(f"'{_escape_sql(r)}'" for r in repo_ids)
            clauses.append(f"repo_id IN ({quoted})")
        if languages:
            quoted = ", ".join(f"'{_escape_sql(lang.lower())}'" for lang in languages)
            clauses.append(f"language IN ({quoted})")
        if path_prefix:
            norm = path_prefix.replace("\\", "/").lstrip("/")
            clauses.append(f"starts_with(rel_path, '{_escape_sql(norm)}')")
        return " AND ".join(clauses) if clauses else None

    def search(
        self,
        *,
        query_text: str,
        query_vector: list[float] | None,
        mode: str = "hybrid",
        repo_ids: list[str] | None = None,
        languages: list[str] | None = None,
        path_prefix: str | None = None,
        top_k: int = 8,
    ) -> list[SearchResult]:
        table = self._open_chunks_table()
        if table is None or table.count_rows() == 0:
            return []

        where_clause = self._build_where_clause(repo_ids, languages, path_prefix)
        repo_roots = {r.repo_id: r.root_path for r in self.list_repos()}
        fetch_k = max(top_k * 3, 20)

        vector_rows: list[dict[str, Any]] = []
        fts_rows: list[dict[str, Any]] = []

        if mode in {"hybrid", "vector"} and query_vector is not None:
            q = table.search(query_vector, vector_column_name="vector").metric("cosine")
            if where_clause:
                q = q.where(where_clause, prefilter=True)
            vector_rows = q.limit(fetch_k).to_list()

        if mode in {"hybrid", "keyword"} and query_text.strip():
            meta = self.get_store_meta()
            if meta.get("fts_dirty", "0") == "1":
                try:
                    self.rebuild_fts_index()
                    table = self._open_chunks_table(force_refresh=True) or table
                except Exception as exc:
                    logger.warning("Lazy FTS rebuild failed: %s", exc)
            try:
                fq = table.search(query_text.strip(), query_type="fts", fts_columns="search_text")
                if where_clause:
                    fq = fq.where(where_clause, prefilter=True)
                fts_rows = fq.limit(fetch_k).to_list()
            except Exception as exc:
                logger.warning("FTS search failed; falling back: %s", exc)
                fts_rows = []

        if mode == "vector":
            return [
                self._row_to_result(row, max(0.0, 1.0 - float(row.get("_distance", 1.0))), repo_roots)
                for row in vector_rows[:top_k]
            ]

        if mode == "keyword":
            max_bm25 = max((float(r.get("_score", 1.0)) for r in fts_rows), default=1.0) or 1.0
            return [
                self._row_to_result(row, float(row.get("_score", 0.0)) / max_bm25, repo_roots)
                for row in fts_rows[:top_k]
            ]

        rrf_k = 60.0
        scores: dict[str, float] = {}
        rows_by_id: dict[str, dict[str, Any]] = {}

        for rank, row in enumerate(vector_rows, start=1):
            cid = row["chunk_id"]
            rows_by_id[cid] = row
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (rrf_k + rank)

        for rank, row in enumerate(fts_rows, start=1):
            cid = row["chunk_id"]
            rows_by_id.setdefault(cid, row)
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (rrf_k + rank)

        if not scores:
            return []

        max_rrf = max(scores.values()) or 1.0
        ranked_ids = sorted(scores.keys(), key=lambda cid: scores[cid], reverse=True)[:top_k]
        return [self._row_to_result(rows_by_id[cid], scores[cid] / max_rrf, repo_roots) for cid in ranked_ids]

    @staticmethod
    def _row_to_result(row: dict[str, Any], score: float, repo_roots: dict[str, str]) -> SearchResult:
        repo_id = row["repo_id"]
        rel_path = row["rel_path"]
        root = repo_roots.get(repo_id, "")
        abs_path = str((Path(root) / rel_path).resolve()) if root else rel_path
        return SearchResult(
            chunk_id=row["chunk_id"],
            repo_id=repo_id,
            rel_path=rel_path,
            abs_path=abs_path,
            language=row["language"],
            symbol_name=row["symbol_name"],
            symbol_kind=row["symbol_kind"],
            start_line=int(row["start_line"]),
            end_line=int(row["end_line"]),
            score=score,
            content=row["content"],
        )

    def get_repo_overview(
        self,
        repo_id: str,
        *,
        max_depth: int = 3,
        max_entries: int = 150,
    ) -> RepoOverview | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM repos WHERE repo_id = ?", (repo_id,)).fetchone()
            if row is None:
                return None
            record = self._row_to_repo(row)
            manifest_rows = self._conn.execute(
                "SELECT rel_path FROM repo_manifests WHERE repo_id = ? ORDER BY rel_path",
                (repo_id,),
            ).fetchall()
            file_rows = self._conn.execute(
                "SELECT rel_path FROM files WHERE repo_id = ? ORDER BY rel_path",
                (repo_id,),
            ).fetchall()

        tree_set: set[str] = set()
        for frow in file_rows:
            parts = str(frow["rel_path"]).split("/")
            if len(parts) <= max_depth:
                tree_set.add(str(frow["rel_path"]))
            else:
                tree_set.add("/".join(parts[:max_depth]) + "/")

        sorted_tree = sorted(tree_set)
        truncated = len(sorted_tree) > max_entries
        return RepoOverview(
            repo_id=record.repo_id,
            name=record.name,
            root_path=record.root_path,
            indexed_at=record.indexed_at,
            file_count=record.file_count,
            chunk_count=record.chunk_count,
            languages=record.languages,
            manifests=[str(r["rel_path"]) for r in manifest_rows],
            tree_entries=sorted_tree[:max_entries],
            truncated=truncated,
        )

    def get_symbol_outline(
        self,
        repo_id: str,
        *,
        path_prefix: str | None = None,
        kind: str | None = None,
        max_symbols: int = 150,
    ) -> FileOutline:
        clauses = ["repo_id = ?"]
        params: list[Any] = [repo_id]
        if path_prefix:
            norm = path_prefix.replace("\\", "/").lstrip("/")
            clauses.append("rel_path LIKE ? ESCAPE '\\'")
            params.append(f"{_escape_like_prefix(norm)}%")
        if kind:
            clauses.append("symbol_kind = ?")
            params.append(kind.strip())

        where_sql = " AND ".join(clauses)
        with self._lock:
            total = int(
                self._conn.execute(f"SELECT COUNT(*) FROM file_symbols WHERE {where_sql}", params).fetchone()[0]
            )
            sym_rows = self._conn.execute(
                f"""
                SELECT rel_path, symbol_name, symbol_kind, signature, docstring_excerpt, start_line, end_line
                FROM file_symbols
                WHERE {where_sql}
                ORDER BY rel_path, start_line
                LIMIT ?
                """,
                [*params, max_symbols],
            ).fetchall()

            imp_clauses = ["repo_id = ?"]
            imp_params: list[Any] = [repo_id]
            if path_prefix:
                norm = path_prefix.replace("\\", "/").lstrip("/")
                imp_clauses.append("rel_path LIKE ? ESCAPE '\\'")
                imp_params.append(f"{_escape_like_prefix(norm)}%")
            imp_rows = self._conn.execute(
                f"""
                SELECT rel_path, import_text
                FROM file_imports
                WHERE {" AND ".join(imp_clauses)}
                ORDER BY rel_path
                LIMIT 200
                """,
                imp_params,
            ).fetchall()

        symbols = [
            SymbolInfo(
                rel_path=str(r["rel_path"]),
                symbol_name=str(r["symbol_name"]),
                symbol_kind=str(r["symbol_kind"]),
                signature=str(r["signature"]),
                docstring_excerpt=str(r["docstring_excerpt"]),
                start_line=int(r["start_line"]),
                end_line=int(r["end_line"]),
            )
            for r in sym_rows
        ]
        imports_by_file: dict[str, list[str]] = {}
        for r in imp_rows:
            imports_by_file.setdefault(str(r["rel_path"]), []).append(str(r["import_text"]))

        return FileOutline(
            repo_id=repo_id,
            total_symbols=total,
            symbols=symbols,
            imports_by_file=imports_by_file,
            truncated=total > max_symbols,
        )

    def total_stats(self) -> dict[str, Any]:
        repos = self.list_repos()
        meta = self.get_store_meta()
        return {
            "data_dir": str(self.data_dir),
            "schema_version": self.schema_version,
            "embed_model": meta.get("embed_model"),
            "vector_dim": meta.get("vector_dim"),
            "fts_dirty": meta.get("fts_dirty", "0") == "1",
            "repos_count": len(repos),
            "total_files": sum(r.file_count for r in repos),
            "total_chunks": sum(r.chunk_count for r in repos),
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
