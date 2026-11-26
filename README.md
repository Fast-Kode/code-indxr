# code-indxr

100% local, cross-platform MCP server and CLI for semantic + keyword code search and deterministic repository orientation, powered by **Ollama**, **Tree-sitter**, **LanceDB**, and **FastMCP**.

Zero source code leaves your machine.

## Why Use `code-indxr`?

- **Deterministic Repo Orientation (`<20ms`):** Gives AI coding agents a token-bounded directory tree, detected manifests (`pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, `pubspec.yaml`, etc.), and class/function symbol outlines before they search.
- **Hybrid RAG Code Search:** Combines cosine vector similarity (`nomic-embed-text` via local Ollama) with LanceDB native BM25 full-text search using Reciprocal Rank Fusion (RRF), plus offline `keyword` fallback.
- **Fast Incremental & Resumable Indexing:** Respects `.gitignore`, lockfiles, and binary files; skips unchanged files via `stat` + `blake2b` hashing; checkpoints batches to disk so interrupted CLI or timed-out MCP runs resume cleanly.
- **Multi-Client & Concurrent-Safe:** Uses OS-level per-repo file locks (`msvcrt` / `fcntl`) and SQLite WAL mode so multiple IDE MCP servers and terminal CLI runs share one local store safely.

## Prerequisites

1. **Python `>=3.11`**
2. **[Ollama](https://ollama.com/)** running locally with an embedding model pulled:

```bash
ollama pull nomic-embed-text
```

## Quickstart

Install globally with `uv` or `pipx` (or `pip install`):

```bash
# Using uv
uv tool install git+https://github.com/samarthakv/code-indxr.git

# Or using pipx
pipx install git+https://github.com/samarthakv/code-indxr.git
```

Verify Ollama, model availability, and local storage:

```bash
code-indxr doctor
```

Cold-index any repository from your terminal (recommended for large repositories):

```bash
code-indxr index /path/to/your/repo
```

Query from the CLI:

```bash
code-indxr map your-repo
code-indxr outline your-repo --path-prefix src/
code-indxr search "refresh JWT token" --repo your-repo
```

## MCP Client Setup

Run `code-indxr serve` over stdio (see [examples/mcp/](examples/mcp/) for copy-paste files).

### Google Antigravity

Add to `~/.gemini/config/mcp_config.json` (Windows: `%USERPROFILE%\.gemini\config\mcp_config.json`):

```json
{
  "mcpServers": {
    "code-indxr": {
      "command": "code-indxr",
      "args": ["serve"],
      "env": {
        "CODE_INDXR_OLLAMA_URL": "http://127.0.0.1:11434",
        "CODE_INDXR_EMBED_MODEL": "nomic-embed-text"
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add --scope user code-indxr -- code-indxr serve
```

### VS Code / GitHub Copilot

Add to `.vscode/mcp.json` (note VS Code uses `"servers"` and `"type": "stdio"`):

```json
{
  "servers": {
    "code-indxr": {
      "type": "stdio",
      "command": "code-indxr",
      "args": ["serve"],
      "env": {
        "CODE_INDXR_OLLAMA_URL": "http://127.0.0.1:11434",
        "CODE_INDXR_EMBED_MODEL": "nomic-embed-text"
      }
    }
  }
}
```

## MCP Tools

| Tool | Purpose |
| :--- | :--- |
| `get_repo_overview(repo, max_depth?, max_entries?)` | Token-bounded directory tree, manifests, and language counts |
| `get_symbol_outline(repo, path_prefix?, kind?, include_docstrings?, max_symbols?)` | Deterministic class/function signatures and imports |
| `search_code(query, repos?, languages?, path_prefix?, top_k?, mode?)` | Cross-repo `hybrid`, `vector`, or `keyword` search |
| `index_repo(path, name?, force?, time_budget_seconds?)` | Incremental, batch-resumable indexing (`complete` or `partial`) |
| `list_repos()` | List indexed repositories and language breakdowns |
| `get_repo_status(repo, fast?)` | Check index state, schema/model info, and changed files |
| `remove_repo(repo)` | Delete a repository and its indexed chunks |
| `health_check()` | Verify Ollama connectivity, model, and store stats |

## Default Storage Locations

| OS | Default `CODE_INDXR_DATA_DIR` |
| :--- | :--- |
| Windows | `%LOCALAPPDATA%\code-indxr` |
| macOS | `~/Library/Application Support/code-indxr` (uses `~/.local/share/code-indxr` if already present) |
| Linux | `$XDG_DATA_HOME/code-indxr` or `~/.local/share/code-indxr` |

## Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `CODE_INDXR_DATA_DIR` | OS default | Directory for `manifest.db`, `lancedb/`, and `locks/` |
| `CODE_INDXR_OLLAMA_URL` | `OLLAMA_HOST` or `http://127.0.0.1:11434` | Local Ollama HTTP base URL |
| `CODE_INDXR_EMBED_MODEL` | `nomic-embed-text` | Ollama embedding model name |
| `CODE_INDXR_BATCH_SIZE` | `32` | Chunks per `/api/embed` request |
| `CODE_INDXR_CONCURRENCY` | `2` | Max concurrent `/api/embed` requests |
| `CODE_INDXR_TIMEOUT` | `120` | HTTP timeout in seconds for Ollama requests |
| `CODE_INDXR_BATCH_FILES` | `50` | Files per disk checkpoint batch |
| `CODE_INDXR_MCP_TIME_BUDGET` | `45` | Soft time budget in seconds for MCP `index_repo` calls (`0` = unlimited) |
| `CODE_INDXR_MAX_FILE_BYTES` | `524288` | Max source file size in bytes (512 KB) |
| `CODE_INDXR_MAX_CHUNK_CHARS` | `2400` | Max characters per AST/window chunk |
| `CODE_INDXR_CHUNK_OVERLAP_LINES` | `6` | Overlap lines for fallback window chunking |
| `CODE_INDXR_LOG_LEVEL` | `WARNING` | `stderr` log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

## Development

```bash
git clone https://github.com/samarthakv/code-indxr.git
cd code-indxr
python -m venv .venv
# Windows: .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
# macOS/Linux: ./.venv/bin/python -m pip install -e ".[dev]"
pytest -v
ruff check .
```

## License

[MIT](LICENSE)
