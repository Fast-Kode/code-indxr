from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import lancedb
from lancedb.index import FTS
import pyarrow as pa

from code_indxr.models import CodeChunk, RepoRecord, SearchResult

CHUNKS_TABLE_NAME = "code_chunks"


def _escape_sql(value: str) -> str:
    return value.replace("'", "''")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip().lower()).strip("-")
    return slug or "repo"


class IndexStore:
    """Embedded SQLite manifest + LanceDB vector & full-text hybrid store."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self._db_path = self.data_dir / "manifest.db"
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()

        self._lance_dir = self.data_dir / "lancedb"
        self._lance_dir.mkdir(parents=True, exist_ok=True)
        self._lance = lancedb.connect(str(self._lance_dir))
        self._fts_ready = False

    def _init_schema(self) -> None:
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
            """
        )
        self._conn.commit()

    def _open_chunks_table(self) -> Any | None:
        listed = self._lance.list_tables()
        names = getattr(listed, "tables", listed)
        if CHUNKS_TABLE_NAME not in names:
            return None
        return self._lance.open_table(CHUNKS_TABLE_NAME)

    def resolve_repo_id(self, root_path: Path, custom_name: str | None = None) -> str:
        """Return existing repo_id for root_path, or allocate a deterministic slug."""
        normalized_root = str(root_path.resolve())
        row = self._conn.execute(
            "SELECT repo_id FROM repos WHERE LOWER(root_path) = LOWER(?)",
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

        suffix = hashlib.blake2b(normalized_root.lower().encode("utf-8"), digest_size=3).hexdigest()
        return f"{base_slug}-{suffix}"

    def get_repo(self, repo_or_path: str) -> RepoRecord | None:
        candidate = repo_or_path.strip()
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
                "SELECT * FROM repos WHERE LOWER(root_path) = LOWER(?)",
                (resolved,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_repo(row)

    def list_repos(self) -> list[RepoRecord]:
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
        now_iso = datetime.now(timezone.utc).isoformat()
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

    def get_file_manifest(self, repo_id: str) -> dict[str, dict[str, Any]]:
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

    def _delete_chunks_for_paths(self, table: Any, repo_id: str, rel_paths: list[str]) -> None:
        if not rel_paths:
            return
        safe_repo = _escape_sql(repo_id)
        batch_size = 100
        for i in range(0, len(rel_paths), batch_size):
            batch = rel_paths[i : i + batch_size]
            quoted = ", ".join(f"'{_escape_sql(p)}'" for p in batch)
            table.delete(f"repo_id = '{safe_repo}' AND rel_path IN ({quoted})")

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
            return

        dim = len(vectors[0])
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
            self._lance.create_table(CHUNKS_TABLE_NAME, data=arrow_table)
        else:
            table.add(arrow_table)
        self._fts_ready = False

    def delete_files(self, repo_id: str, rel_paths: list[str]) -> None:
        if not rel_paths:
            return
        table = self._open_chunks_table()
        if table is not None:
            self._delete_chunks_for_paths(table, repo_id, rel_paths)
        self._conn.executemany(
            "DELETE FROM files WHERE repo_id = ? AND rel_path = ?",
            [(repo_id, p) for p in rel_paths],
        )
        self._conn.commit()
        self._fts_ready = False

    def delete_repo(self, repo_id: str) -> bool:
        existing = self._conn.execute("SELECT 1 FROM repos WHERE repo_id = ?", (repo_id,)).fetchone()
        if existing is None:
            return False

        table = self._open_chunks_table()
        if table is not None:
            table.delete(f"repo_id = '{_escape_sql(repo_id)}'")

        self._conn.execute("DELETE FROM files WHERE repo_id = ?", (repo_id,))
        self._conn.execute("DELETE FROM repos WHERE repo_id = ?", (repo_id,))
        self._conn.commit()
        self._fts_ready = False
        return True

    def rebuild_fts_index(self) -> None:
        """Create or refresh native LanceDB full-text index on search_text."""
        table = self._open_chunks_table()
        if table is None or table.count_rows() == 0:
            self._fts_ready = False
            return
        table.create_index("search_text", config=FTS(), replace=True)
        self._fts_ready = True

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
            clauses.append(f"rel_path LIKE '{_escape_sql(norm)}%'")
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
            if not self._fts_ready:
                try:
                    self.rebuild_fts_index()
                except Exception:
                    pass
            try:
                fq = table.search(query_text.strip(), query_type="fts", fts_columns="search_text")
                if where_clause:
                    fq = fq.where(where_clause, prefilter=True)
                fts_rows = fq.limit(fetch_k).to_list()
            except Exception:
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

        # Hybrid: Reciprocal Rank Fusion (RRF) across vector + BM25 results
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
        return [
            self._row_to_result(rows_by_id[cid], scores[cid] / max_rrf, repo_roots)
            for cid in ranked_ids
        ]

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

    def total_stats(self) -> dict[str, Any]:
        repos = self.list_repos()
        return {
            "data_dir": str(self.data_dir),
            "repos_count": len(repos),
            "total_files": sum(r.file_count for r in repos),
            "total_chunks": sum(r.chunk_count for r in repos),
        }

    def close(self) -> None:
        self._conn.close()
