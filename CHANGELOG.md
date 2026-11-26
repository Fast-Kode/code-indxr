# Changelog

All notable changes to this project will be documented in this file.

## [0.2.0] - 2026-11-26

### Added
- Unified `code-indxr` CLI (`serve`, `index`, `search`, `map`, `outline`, `list`, `status`, `remove`, `doctor`) and `python -m code_indxr` entry point; bare `code-indxr` prints help in an interactive TTY and starts stdio MCP when piped.
- Cross-process OS advisory file locks (`msvcrt.locking` on Windows, `fcntl.flock` on POSIX) and SQLite `busy_timeout` + `threading.RLock` for safe concurrent CLI and multi-IDE MCP access.
- Numbered SQLite migrations (`PRAGMA user_version = 2`) with automatic `manifest.db.bak.v1` backup and `(embed_model, vector_dim)` compatibility checks.
- Coarse batch-resumable indexing (`batch_files=50`, `mcp_time_budget_seconds=45.0`) with `partial` status and FastMCP progress reporting.
- Single-pass Tree-sitter extraction of deterministic symbol signatures, docstrings, and imports for `<20ms` `get_repo_overview` and `get_symbol_outline` tools.
- Canonical `repo` MCP tools (`index_repo`, `list_repos`, `get_repo_status`, `remove_repo`) with deprecated `*project*` aliases for `0.1.0` compatibility.
- Cross-platform data directory defaults (`%LOCALAPPDATA%`, macOS `Application Support` with legacy fallback, `$XDG_DATA_HOME`), `OLLAMA_HOST` fallback, MIT `LICENSE`, multi-client MCP examples, and 3-OS GitHub Actions CI.

## [0.1.0] - 2026-11-26

### Added
- Initial local AST-aware hybrid RAG code indexer and FastMCP stdio server powered by Ollama (`nomic-embed-text`), `tree-sitter`, and `LanceDB`.
