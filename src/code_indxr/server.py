from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import sys
from typing import Any, AsyncIterator, Literal

from fastmcp import FastMCP

from code_indxr.config import Settings
from code_indxr.embedder import OllamaEmbedder
from code_indxr.indexer import CodeIndexer
from code_indxr.store import IndexStore

logging.basicConfig(
    level=logging.WARNING,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("code_indxr")

SERVER_INSTRUCTIONS = (
    "Local AST-aware hybrid RAG code indexer powered by Ollama and LanceDB. "
    "Call index_project(path) when starting work on a new or modified repository—incremental "
    "re-indexing skips unchanged files in ~1-3 seconds. Use search_code(...) to retrieve "
    "relevant functions, classes, and snippets across one or multiple indexed repositories "
    "using hybrid (semantic vector + BM25 keyword), vector, or keyword search."
)


def _build_default_indexer() -> CodeIndexer:
    settings = Settings.from_env()
    store = IndexStore(settings.data_dir)
    embedder = OllamaEmbedder(
        base_url=settings.ollama_url,
        model=settings.embed_model,
        batch_size=settings.embed_batch_size,
        concurrency=settings.embed_concurrency,
        timeout=settings.embed_timeout_seconds,
    )
    return CodeIndexer(settings=settings, store=store, embedder=embedder)


def create_server(indexer: CodeIndexer | None = None) -> FastMCP:
    """Create and configure the FastMCP server with all code-indexing tools."""
    owns_indexer = indexer is None
    active_indexer = indexer or _build_default_indexer()

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owns_indexer:
                await active_indexer.close()

    mcp = FastMCP(name="code-indxr", instructions=SERVER_INSTRUCTIONS, lifespan=lifespan)

    @mcp.tool()
    async def index_project(
        path: str,
        name: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Index or incrementally refresh a local project directory for semantic & keyword RAG search.

        Args:
            path: Absolute or relative filesystem path to the project root directory.
            name: Optional friendly project name / repo_id alias. Defaults to folder name.
            force: If True, re-chunk and re-embed all files ignoring the incremental cache.
        """
        stats = await active_indexer.index_project(path, name=name, force=force)
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
        results = await active_indexer.search_code(
            query,
            repos=repos,
            languages=languages,
            path_prefix=path_prefix,
            top_k=top_k,
            mode=mode,
        )
        return {
            "query": query,
            "mode": mode,
            "count": len(results),
            "results": [r.to_dict() for r in results],
        }

    @mcp.tool()
    def list_indexed_projects() -> dict[str, Any]:
        """List all indexed repositories with paths, file/chunk counts, and language breakdowns."""
        projects = active_indexer.list_projects()
        return {
            "count": len(projects),
            "projects": [p.to_dict() for p in projects],
        }

    @mcp.tool()
    def get_project_status(repo: str) -> dict[str, Any]:
        """Check whether a project (repo_id or filesystem path) is indexed and how many files changed on disk."""
        return active_indexer.get_project_status(repo).to_dict()

    @mcp.tool()
    def remove_project(repo: str) -> dict[str, Any]:
        """Remove an indexed project (by repo_id or filesystem path) and delete all of its vectors."""
        removed = active_indexer.remove_project(repo)
        return {"repo": repo, "removed": removed}

    @mcp.tool()
    async def health_check() -> dict[str, Any]:
        """Check local Ollama connectivity, embedding model availability, and LanceDB index storage stats."""
        return await active_indexer.health_check()

    return mcp


def main() -> None:
    mcp = create_server()
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
