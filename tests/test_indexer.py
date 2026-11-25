from __future__ import annotations

from pathlib import Path

from code_indxr.config import Settings
from code_indxr.indexer import CodeIndexer
from code_indxr.store import IndexStore
from tests.conftest import FakeEmbedder


async def test_indexer_full_and_incremental_lifecycle(
    sample_repo: Path,
    tmp_settings: Settings,
    fake_embedder: FakeEmbedder,
) -> None:
    store = IndexStore(tmp_settings.data_dir)
    indexer = CodeIndexer(tmp_settings, store, fake_embedder)
    try:
        # 1. Cold index
        stats1 = await indexer.index_project(sample_repo)
        assert stats1.files_scanned == 5
        assert stats1.files_indexed == 5
        assert stats1.files_skipped == 0
        assert stats1.chunks_indexed >= 5
        initial_embed_calls = len(fake_embedder.document_calls)
        assert initial_embed_calls >= 1

        # 2. Immediate re-index with zero changes -> skips all files, zero embed calls
        stats2 = await indexer.index_project(sample_repo)
        assert stats2.files_indexed == 0
        assert stats2.files_skipped == 5
        assert len(fake_embedder.document_calls) == initial_embed_calls

        # 3. Modify 1 file, add 1 file, delete 1 file
        (sample_repo / "backend" / "token_service.py").write_text(
            "def revoke_session(session_id: str) -> bool:\n    return True\n",
            encoding="utf-8",
        )
        (sample_repo / "backend" / "new_router.py").write_text(
            "def get_health_route() -> str:\n    return '/healthz'\n",
            encoding="utf-8",
        )
        (sample_repo / "android" / "app" / "CryptoHelper.java").unlink()

        status_before = indexer.get_project_status(str(sample_repo))
        assert status_before.indexed is True
        assert status_before.stale_files == 1
        assert status_before.new_files == 1
        assert status_before.deleted_files == 1

        stats3 = await indexer.index_project(sample_repo)
        assert stats3.files_indexed == 2
        assert stats3.files_removed == 1
        assert stats3.files_skipped == 3

        # 4. Search across indexed files
        hits = await indexer.search_code("revoke_session", repos=[str(sample_repo)], mode="hybrid")
        assert hits
        assert hits[0].rel_path == "backend/token_service.py"

        # Deleted file must no longer appear
        deleted_hits = await indexer.search_code("hashPassword", mode="keyword")
        assert all(h.rel_path != "android/app/CryptoHelper.java" for h in deleted_hits)

        # 5. List & remove
        projects = indexer.list_projects()
        assert len(projects) == 1
        assert projects[0].repo_id == stats1.repo_id

        assert indexer.remove_project(stats1.repo_id) is True
        assert indexer.list_projects() == []
    finally:
        await indexer.close()
