from __future__ import annotations

from pathlib import Path

from code_indxr.chunker import extract_file_artifacts
from code_indxr.config import Settings
from code_indxr.indexer import CodeIndexer
from code_indxr.store import IndexStore
from tests.conftest import FakeEmbedder


def test_single_pass_extracts_class_methods_signatures_docstrings_and_imports() -> None:
    source = '''import os
from pathlib import Path

class UserService:
    """Service managing user records."""

    def get_user(self, user_id: str) -> dict:
        """Fetch user by ID."""
        return {"id": user_id}

    def delete_user(self, user_id: str) -> bool:
        return True
'''
    artifacts = extract_file_artifacts("repo1", "app/users.py", source, "python")
    assert artifacts.used_fallback is False
    assert any("import os" in imp for imp in artifacts.imports)
    assert any("from pathlib import Path" in imp for imp in artifacts.imports)

    sym_names = {s.symbol_name for s in artifacts.symbols}
    assert "UserService" in sym_names
    assert "UserService.get_user" in sym_names
    assert "UserService.delete_user" in sym_names

    get_user_sym = next(s for s in artifacts.symbols if s.symbol_name == "UserService.get_user")
    assert "def get_user(self, user_id: str) -> dict:" in get_user_sym.signature
    assert "Fetch user by ID." in get_user_sym.docstring_excerpt


async def test_repo_overview_and_symbol_outline_queries(
    sample_repo: Path,
    tmp_settings: Settings,
    fake_embedder: FakeEmbedder,
) -> None:
    (sample_repo / "pyproject.toml").write_text('[project]\nname = "sample"\n', encoding="utf-8")
    (sample_repo / "package.json").write_text('{"name": "sample-ui"}\n', encoding="utf-8")

    store = IndexStore(tmp_settings.data_dir, embed_model=tmp_settings.embed_model)
    indexer = CodeIndexer(tmp_settings, store, fake_embedder)
    try:
        stats = await indexer.index_repo(sample_repo)
        assert stats.status == "complete"

        overview = indexer.get_repo_overview(stats.repo_id, max_depth=3, max_entries=50)
        assert overview.repo_id == stats.repo_id
        assert "pyproject.toml" in overview.manifests
        assert "package.json" in overview.manifests
        assert "python" in overview.languages
        assert overview.tree_entries

        outline = indexer.get_symbol_outline(
            stats.repo_id,
            path_prefix="backend/",
            max_symbols=50,
        )
        assert outline.total_symbols >= 2
        assert any(s.symbol_name == "TokenService" for s in outline.symbols)
        assert any("TokenService.refresh_access_token" == s.symbol_name for s in outline.symbols)
    finally:
        await indexer.close()
