from __future__ import annotations

from pathlib import Path

from code_indxr.models import CodeChunk
from code_indxr.store import IndexStore


def _make_chunk(
    repo_id: str,
    rel_path: str,
    symbol: str,
    content: str,
    language: str = "python",
    start_line: int = 1,
) -> CodeChunk:
    return CodeChunk(
        chunk_id=f"{repo_id}:{rel_path}:{start_line}",
        repo_id=repo_id,
        rel_path=rel_path,
        language=language,
        symbol_name=symbol,
        symbol_kind="function",
        start_line=start_line,
        end_line=start_line + 5,
        content=content,
    )


def test_store_upsert_hybrid_vector_keyword_and_delete(tmp_path: Path) -> None:
    store = IndexStore(tmp_path / "store-data")
    try:
        repo_path = (tmp_path / "repo-a").resolve()
        repo_path.mkdir()
        repo_id = store.resolve_repo_id(repo_path)

        c1 = _make_chunk(
            repo_id,
            "src/auth.py",
            "refresh_jwt_token",
            "def refresh_jwt_token(token): return 'new_jwt_' + token",
        )
        c2 = _make_chunk(
            repo_id,
            "src/billing.ts",
            "calculateInvoiceTotal",
            "export function calculateInvoiceTotal(items) { return items.length * 10; }",
            language="typescript",
        )

        store.upsert_repo_record(
            repo_id=repo_id,
            name="repo-a",
            root_path=str(repo_path),
            file_count=2,
            chunk_count=2,
            languages={"python": 1, "typescript": 1},
        )
        store.upsert_chunks(
            repo_id=repo_id,
            replaced_rel_paths=["src/auth.py", "src/billing.ts"],
            chunks=[c1, c2],
            vectors=[[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]],
        )
        store.rebuild_fts_index()

        # Vector search
        vec_hits = store.search(
            query_text="jwt refresh",
            query_vector=[0.98, 0.02, 0.0, 0.0],
            mode="vector",
            top_k=2,
        )
        assert vec_hits
        assert vec_hits[0].symbol_name == "refresh_jwt_token"

        # Keyword search (no vector needed)
        kw_hits = store.search(
            query_text="calculateInvoiceTotal",
            query_vector=None,
            mode="keyword",
            top_k=2,
        )
        assert kw_hits
        assert kw_hits[0].symbol_name == "calculateInvoiceTotal"

        # Hybrid search + language filter
        hybrid_hits = store.search(
            query_text="calculateInvoiceTotal",
            query_vector=[0.0, 1.0, 0.0, 0.0],
            mode="hybrid",
            languages=["typescript"],
            top_k=5,
        )
        assert len(hybrid_hits) == 1
        assert hybrid_hits[0].rel_path == "src/billing.ts"

        # Delete single file
        store.delete_files(repo_id, ["src/auth.py"])
        store.rebuild_fts_index()
        after_del = store.search(
            query_text="refresh_jwt_token",
            query_vector=[1.0, 0.0, 0.0, 0.0],
            mode="hybrid",
            top_k=5,
        )
        assert all(h.rel_path != "src/auth.py" for h in after_del)

        # Delete entire repo
        assert store.delete_repo(repo_id) is True
        assert store.list_repos() == []
    finally:
        store.close()
