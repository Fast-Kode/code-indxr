from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastmcp import Context, FastMCP

from code_indxr.app import build_indexer, configure_logging
from code_indxr.config import Settings
from code_indxr.indexer import CodeIndexer

SERVER_INSTRUCTIONS = (
    "Local AST-aware hybrid RAG code indexer powered by Ollama and LanceDB. "
    "Recommended workflow in any unfamiliar repository: "
    "1) Call get_repo_overview(repo) to see directory layout, manifests, and languages. "
    "2) Call get_symbol_outline(repo, path_prefix=...) for classes/functions/signatures. "
    "3) Call search_code(...) for hybrid (semantic vector + BM25 keyword) code chunks. "
    "4) Call index_repo(path) when starting work on a new or modified repo; incremental "
    "runs skip unchanged files in under a second, and large cold runs checkpoint in batches."
)


def create_server(indexer: CodeIndexer | None = None) -> FastMCP:
    """Create and configure the FastMCP server with all code-indexing tools."""
    owns_indexer = indexer is None
    active_indexer = indexer or build_indexer()

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owns_indexer:
                await active_indexer.close()

    mcp = FastMCP(name="code-indxr", instructions=SERVER_INSTRUCTIONS, lifespan=lifespan)

    @mcp.tool()
    async def index_repo(
        path: str,
        name: str | None = None,
        force: bool = False,
        time_budget_seconds: float | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Index or incrementally refresh a local repository for hybrid RAG search and outlines.

        Args:
            path: Absolute or relative filesystem path to the repository root directory.
            name: Optional friendly display name / repo_id alias. Defaults to folder name.
            force: If True, re-chunk and re-embed all files ignoring the incremental cache.
            time_budget_seconds: Optional soft cutoff in seconds (defaults to CODE_INDXR_MCP_TIME_BUDGET=45s).
        """
        effective_budget = (
            active_indexer.settings.mcp_time_budget_seconds if time_budget_seconds is None else time_budget_seconds
        )

        async def _report(done: int, total: int, message: str) -> None:
            if ctx is not None and total > 0:
                await ctx.report_progress(progress=done, total=total, message=message)

        stats = await active_indexer.index_repo(
            path,
            name=name,
            force=force,
            time_budget_seconds=effective_budget,
            progress_cb=_report,
        )
        return stats.to_dict()

    @mcp.tool()
    async def index_project(
        path: str,
        name: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Deprecated alias for index_repo."""
        stats = await active_indexer.index_repo(
            path,
            name=name,
            force=force,
            time_budget_seconds=active_indexer.settings.mcp_time_budget_seconds,
        )
        return stats.to_dict()

    @mcp.tool()
    async def search_code(
        query: str,
        repos: list[str] | None = None,
        languages: list[str] | None = None,
        path_prefix: str | None = None,
        top_k: int = 8,
        mode: Literal["hybrid", "vector", "keyword"] = "hybrid",
    ) -> dict[str, Any]:
        """Search indexed repositories for relevant code chunks using hybrid RAG.

        Args:
            query: Natural language question, symbol name, error message, or code pattern.
            repos: Optional list of repo_ids or filesystem paths to restrict search. Omit to search all repos.
            languages: Optional language filter (e.g. ["tsx", "typescript", "python", "kotlin", "java", "dart"]).
            path_prefix: Optional relative path prefix filter (e.g. "src/auth/").
            top_k: Maximum results to return (1-50, default 8).
            mode: "hybrid" (vector + BM25 RRF, recommended), "vector" (semantic only), or "keyword" (instant BM25).
        """
        results, effective_mode, degraded_modes, warnings = await active_indexer.search_code_detailed(
            query,
            repos=repos,
            languages=languages,
            path_prefix=path_prefix,
            top_k=top_k,
            mode=mode,
        )
        payload: dict[str, Any] = {
            "query": query,
            "mode": effective_mode,
            "count": len(results),
            "results": [r.to_dict() for r in results],
        }
        if degraded_modes:
            payload["degraded_modes"] = degraded_modes
        if warnings:
            payload["warnings"] = warnings
        return payload

    @mcp.tool()
    async def get_repo_overview(
        repo: str,
        max_depth: int = 3,
        max_entries: int = 150,
    ) -> dict[str, Any]:
        """Return a token-bounded directory tree, language breakdown, and detected manifests for an indexed repo."""
        overview = await asyncio.to_thread(
            active_indexer.get_repo_overview,
            repo,
            max_depth=max_depth,
            max_entries=max_entries,
        )
        return overview.to_dict()

    @mcp.tool()
    async def get_symbol_outline(
        repo: str,
        path_prefix: str | None = None,
        kind: str | None = None,
        include_docstrings: bool = False,
        max_symbols: int = 150,
    ) -> dict[str, Any]:
        """Return deterministic class/function signatures and imports for an indexed repo."""
        outline = await asyncio.to_thread(
            active_indexer.get_symbol_outline,
            repo,
            path_prefix=path_prefix,
            kind=kind,
            max_symbols=max_symbols,
        )
        return outline.to_dict(include_docstrings=include_docstrings)

    @mcp.tool()
    def list_repos() -> dict[str, Any]:
        """List all indexed repositories with paths, file/chunk counts, and language breakdowns."""
        repos = active_indexer.list_repos()
        return {
            "count": len(repos),
            "projects": [p.to_dict() for p in repos],
            "repos": [p.to_dict() for p in repos],
        }

    @mcp.tool()
    def list_indexed_projects() -> dict[str, Any]:
        """Deprecated alias for list_repos."""
        return list_repos()

    @mcp.tool()
    async def get_repo_status(repo: str, fast: bool = False) -> dict[str, Any]:
        """Check whether a repository (repo_id or filesystem path) is indexed and how many files changed on disk."""
        status = await asyncio.to_thread(active_indexer.get_repo_status, repo, fast=fast)
        return status.to_dict()

    @mcp.tool()
    async def get_project_status(repo: str) -> dict[str, Any]:
        """Deprecated alias for get_repo_status."""
        status = await asyncio.to_thread(active_indexer.get_repo_status, repo)
        return status.to_dict()

    @mcp.tool()
    def remove_repo(repo: str) -> dict[str, Any]:
        """Remove an indexed repository (by repo_id or filesystem path) and delete all of its vectors."""
        removed = active_indexer.remove_repo(repo)
        return {"repo": repo, "removed": removed}

    @mcp.tool()
    def remove_project(repo: str) -> dict[str, Any]:
        """Deprecated alias for remove_repo."""
        return remove_repo(repo)

    @mcp.tool()
    async def health_check() -> dict[str, Any]:
        """Check local Ollama connectivity, embedding model availability, and LanceDB/SQLite store stats."""
        return await active_indexer.health_check()

    return mcp


def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    mcp = create_server(build_indexer(settings))
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
