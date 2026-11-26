from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import lancedb
import pyarrow as pa
import pytest

from code_indxr.store import CHUNKS_TABLE_NAME, IndexStore, StoreCompatibilityError


def _create_v1_store(data_dir: Path, root_path: Path, dim: int = 4) -> str:
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_dir / "manifest.db")
    conn.executescript(
        """
        CREATE TABLE repos (
            repo_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            root_path TEXT NOT NULL UNIQUE,
            indexed_at TEXT NOT NULL,
            file_count INTEGER NOT NULL DEFAULT 0,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            languages_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE files (
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
    repo_id = "legacy-repo"
    conn.execute(
        "INSERT INTO repos VALUES (?, ?, ?, ?, ?, ?, ?)",
        (repo_id, "legacy-repo", str(root_path), "2026-01-01T00:00:00Z", 1, 1, json.dumps({"python": 1})),
    )
    conn.execute(
        "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?)",
        (repo_id, "app.py", 123456, 42, "abc123", "python", 1),
    )
    conn.commit()
    conn.close()

    lance_dir = data_dir / "lancedb"
    lance_dir.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(lance_dir))
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
    table = pa.Table.from_pylist(
        [
            {
                "chunk_id": "legacy:app.py:1",
                "repo_id": repo_id,
                "rel_path": "app.py",
                "language": "python",
                "symbol_name": "greet",
                "symbol_kind": "function_definition",
                "start_line": 1,
                "end_line": 2,
                "content": "def greet(): return 'hi'",
                "search_text": "app.py greet def greet(): return 'hi'",
                "vector": [1.0, 0.0, 0.0, 0.0],
            }
        ],
        schema=schema,
    )
    db.create_table(CHUNKS_TABLE_NAME, data=table)
    return repo_id


def test_v1_migration_preserves_vectors_and_creates_backup(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    repo_root = tmp_path / "legacy-repo"
    repo_root.mkdir()
    repo_id = _create_v1_store(data_dir, repo_root, dim=4)

    store = IndexStore(data_dir, embed_model="nomic-embed-text")
    try:
        assert (data_dir / "manifest.db.bak.v1").exists()
        assert store.schema_version == 2
        meta = store.get_store_meta()
        assert meta["vector_dim"] == "4"
        assert meta["embed_model"] == "nomic-embed-text"

        repos = store.list_repos()
        assert len(repos) == 1
        assert repos[0].repo_id == repo_id

        hits = store.search(
            query_text="greet",
            query_vector=[1.0, 0.0, 0.0, 0.0],
            mode="hybrid",
            top_k=3,
        )
        assert hits
        assert hits[0].symbol_name == "greet"
    finally:
        store.close()


def test_model_and_dimension_mismatch_fail_fast(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    repo_root = tmp_path / "legacy-repo"
    repo_root.mkdir()
    _create_v1_store(data_dir, repo_root, dim=4)

    store = IndexStore(data_dir, embed_model="nomic-embed-text")
    store.close()

    with pytest.raises(StoreCompatibilityError) as exc_info:
        IndexStore(data_dir, embed_model="mxbai-embed-large")
    assert "nomic-embed-text" in str(exc_info.value)


def test_newer_schema_version_refuses_downgrade(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    conn = sqlite3.connect(data_dir / "manifest.db")
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()

    with pytest.raises(StoreCompatibilityError):
        IndexStore(data_dir, embed_model="nomic-embed-text")
