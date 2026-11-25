from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastmcp import Client

from code_indxr.config import Settings
from code_indxr.indexer import CodeIndexer
from code_indxr.server import create_server
from code_indxr.store import IndexStore
from tests.conftest import FakeEmbedder


def _decode_tool_result(result: Any) -> dict[str, Any]:
    if hasattr(result, "structured_content") and result.structured_content is not None:
        return dict(result.structured_content)
    if hasattr(result, "data") and isinstance(result.data, dict):
        return result.data
    content = getattr(result, "content", result)
    if isinstance(content, list) and content:
        text = getattr(content[0], "text", None)
        if isinstance(text, str):
            return json.loads(text)
    raise AssertionError(f"Unable to decode MCP tool result: {result!r}")


async def test_mcp_server_end_to_end_tools(
    sample_repo: Path,
    tmp_settings: Settings,
    fake_embedder: FakeEmbedder,
) -> None:
    store = IndexStore(tmp_settings.data_dir)
    indexer = CodeIndexer(tmp_settings, store, fake_embedder)
    mcp = create_server(indexer)

    try:
        async with Client(mcp) as client:
            tools = await client.list_tools()
            tool_names = {t.name for t in tools}
            assert {
                "index_project",
                "search_code",
                "list_indexed_projects",
                "get_project_status",
                "remove_project",
                "health_check",
            }.issubset(tool_names)

            health = _decode_tool_result(await client.call_tool("health_check", {}))
            assert health["ok"] is True

            idx_res = _decode_tool_result(
                await client.call_tool("index_project", {"path": str(sample_repo)})
            )
            assert idx_res["files_indexed"] == 5
            repo_id = idx_res["repo_id"]

            listed = _decode_tool_result(await client.call_tool("list_indexed_projects", {}))
            assert listed["count"] == 1
            assert listed["projects"][0]["repo_id"] == repo_id

            status = _decode_tool_result(
                await client.call_tool("get_project_status", {"repo": repo_id})
            )
            assert status["indexed"] is True
            assert status["stale_files"] == 0

            search_res = _decode_tool_result(
                await client.call_tool(
                    "search_code",
                    {
                        "query": "AuthCard logout button",
                        "repos": [repo_id],
                        "languages": ["tsx"],
                        "top_k": 3,
                        "mode": "hybrid",
                    },
                )
            )
            assert search_res["count"] >= 1
            assert search_res["results"][0]["file_path"] == "src/components/AuthCard.tsx"

            removed = _decode_tool_result(
                await client.call_tool("remove_project", {"repo": repo_id})
            )
            assert removed["removed"] is True

            listed_after = _decode_tool_result(await client.call_tool("list_indexed_projects", {}))
            assert listed_after["count"] == 0
    finally:
        await indexer.close()
