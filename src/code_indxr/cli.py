from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any

from code_indxr.app import build_indexer, configure_logging
from code_indxr.config import Settings


def _print_output(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            print(f"{key}: {value}")
    else:
        print(payload)


def _run_serve() -> int:
    from code_indxr.server import main as serve_main

    serve_main()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-indxr",
        description="Local AST-aware hybrid RAG code indexer and MCP server powered by Ollama and LanceDB.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable INFO logging to stderr.")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("serve", help="Start the FastMCP stdio server.")

    p_index = subparsers.add_parser("index", help="Index or incrementally refresh a repository.")
    p_index.add_argument("path", help="Path to the repository root directory.")
    p_index.add_argument("--name", default=None, help="Optional friendly repository name / repo_id.")
    p_index.add_argument("--force", action="store_true", help="Re-index all files ignoring incremental cache.")
    p_index.add_argument("--batch-size", type=int, default=None, help="Files per checkpoint batch (default 50).")
    p_index.add_argument("--json", action="store_true", help="Output JSON.")

    p_search = subparsers.add_parser("search", help="Search indexed repositories.")
    p_search.add_argument("query", help="Search query or symbol name.")
    p_search.add_argument("--repo", action="append", dest="repos", default=None, help="Filter by repo_id or path.")
    p_search.add_argument("--lang", action="append", dest="languages", default=None, help="Filter by language.")
    p_search.add_argument("--path-prefix", default=None, help="Filter by relative path prefix.")
    p_search.add_argument("--top-k", type=int, default=8, help="Max results (default 8).")
    p_search.add_argument("--mode", choices=["hybrid", "vector", "keyword"], default="hybrid", help="Search mode.")
    p_search.add_argument("--json", action="store_true", help="Output JSON.")

    p_map = subparsers.add_parser("map", help="Show deterministic repository overview.")
    p_map.add_argument("repo", help="Repository ID or path.")
    p_map.add_argument("--depth", type=int, default=3, help="Max directory tree depth (default 3).")
    p_map.add_argument("--max-entries", type=int, default=150, help="Max tree entries (default 150).")
    p_map.add_argument("--json", action="store_true", help="Output JSON.")

    p_outline = subparsers.add_parser("outline", help="Show deterministic symbol outline.")
    p_outline.add_argument("repo", help="Repository ID or path.")
    p_outline.add_argument("--path-prefix", default=None, help="Filter by relative path prefix.")
    p_outline.add_argument("--kind", default=None, help="Filter by AST symbol kind.")
    p_outline.add_argument("--docstrings", action="store_true", help="Include docstring excerpts.")
    p_outline.add_argument("--max-symbols", type=int, default=150, help="Max symbols (default 150).")
    p_outline.add_argument("--json", action="store_true", help="Output JSON.")

    p_list = subparsers.add_parser("list", help="List indexed repositories.")
    p_list.add_argument("--json", action="store_true", help="Output JSON.")

    p_status = subparsers.add_parser("status", help="Show index status for a repository.")
    p_status.add_argument("repo", help="Repository ID or path.")
    p_status.add_argument("--fast", action="store_true", help="Skip disk scan and return cached metadata.")
    p_status.add_argument("--json", action="store_true", help="Output JSON.")

    p_remove = subparsers.add_parser("remove", help="Remove an indexed repository.")
    p_remove.add_argument("repo", help="Repository ID or path.")
    p_remove.add_argument("--json", action="store_true", help="Output JSON.")

    p_doctor = subparsers.add_parser("doctor", help="Check Ollama, model availability, and store health.")
    p_doctor.add_argument("--json", action="store_true", help="Output JSON.")

    return parser


async def _dispatch_async(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    configure_logging("INFO" if args.verbose else settings.log_level)
    indexer = build_indexer(settings)
    try:
        if args.command == "index":
            stats = await indexer.index_repo(
                args.path,
                name=args.name,
                force=args.force,
                batch_files=args.batch_size,
                time_budget_seconds=None,
            )
            _print_output(stats.to_dict(), args.json)
            return 0

        if args.command == "search":
            results, effective_mode, degraded, warnings = await indexer.search_code_detailed(
                args.query,
                repos=args.repos,
                languages=args.languages,
                path_prefix=args.path_prefix,
                top_k=args.top_k,
                mode=args.mode,
            )
            payload: dict[str, Any] = {
                "query": args.query,
                "mode": effective_mode,
                "count": len(results),
                "results": [r.to_dict() for r in results],
            }
            if degraded:
                payload["degraded_modes"] = degraded
            if warnings:
                payload["warnings"] = warnings
            if args.json:
                _print_output(payload, True)
            else:
                for item in payload["results"]:
                    print(
                        f"[{item['repo_id']}] {item['file_path']}:{item['lines']} {item['symbol']} (score={item['score']})"
                    )
            return 0

        if args.command == "map":
            overview = indexer.get_repo_overview(args.repo, max_depth=args.depth, max_entries=args.max_entries)
            _print_output(overview.to_dict(), args.json)
            return 0

        if args.command == "outline":
            outline = indexer.get_symbol_outline(
                args.repo,
                path_prefix=args.path_prefix,
                kind=args.kind,
                max_symbols=args.max_symbols,
            )
            payload = outline.to_dict(include_docstrings=args.docstrings)
            if args.json:
                _print_output(payload, True)
            else:
                for sym in payload["symbols"]:
                    print(f"{sym['file_path']}:{sym['lines']} [{sym['kind']}] {sym['signature']}")
            return 0

        if args.command == "list":
            repos = indexer.list_repos()
            payload = {"count": len(repos), "repos": [r.to_dict() for r in repos]}
            _print_output(payload, args.json)
            return 0

        if args.command == "status":
            status = indexer.get_repo_status(args.repo, fast=args.fast)
            _print_output(status.to_dict(), args.json)
            return 0

        if args.command == "remove":
            removed = indexer.remove_repo(args.repo)
            _print_output({"repo": args.repo, "removed": removed}, args.json)
            return 0

        if args.command == "doctor":
            health = await indexer.health_check()
            _print_output(health, args.json)
            return 0 if health.get("ok") else 1

        return 1
    finally:
        await indexer.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)

    if not raw_argv:
        if sys.stdin.isatty():
            parser.print_help()
            return 0
        return _run_serve()

    args = parser.parse_args(raw_argv)
    if args.command == "serve":
        return _run_serve()

    try:
        return asyncio.run(_dispatch_async(args))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
