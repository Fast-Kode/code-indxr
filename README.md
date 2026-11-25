# code-indxr

Local AST-aware hybrid RAG code indexer and MCP server powered by **Ollama** (`nomic-embed-text`), **Tree-sitter**, **LanceDB**, and **FastMCP**.

Built for AI coding agents (such as Google Antigravity) to quickly index local repositories on demand and run semantic + keyword (BM25) hybrid search across one or multiple projects without external cloud services.

## Features

- **AST-Aware Chunking**: Splits code at function/class/declaration boundaries using `tree-sitter-language-pack` (TypeScript/TSX/React, JavaScript, Python, Kotlin, Java, Dart/Flutter, Go, Rust, etc.) with line-window fallback for configs and docs.
- **Fast Incremental Indexing**: Tracks file `mtime`, size, and `blake2b` hashes in SQLite (`manifest.db`) while respecting `.gitignore`, lockfiles, and build directories. Unchanged files are skipped in milliseconds.
- **Hybrid RAG Search**: Combines cosine vector similarity and LanceDB native full-text search (BM25) via Reciprocal Rank Fusion (RRF), plus pure `vector` and instant `keyword` modes.
- **100% Local**: Uses local Ollama `/api/embed` batching and an embedded LanceDB store (`%LOCALAPPDATA%\code-indxr` by default).

## Prerequisites

- Python `>=3.11` (configured for `3.12.10` via `.python-version`)
- [Ollama](https://ollama.com/) running locally with `nomic-embed-text`:

```powershell
ollama pull nomic-embed-text
```

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Run tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

## MCP Registration (Antigravity)

Add to `%USERPROFILE%\.gemini\config\mcp_config.json`:

```json
{
  "mcpServers": {
    "code-indxr": {
      "command": "D:\\Projects\\python\\code-indxr\\.venv\\Scripts\\python.exe",
      "args": ["D:\\Projects\\python\\code-indxr\\main.py"],
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
| `index_project(path, name?, force?)` | Cold-index or incrementally refresh any local repository |
| `search_code(query, repos?, languages?, path_prefix?, top_k?, mode?)` | Cross-repo `hybrid`, `vector`, or `keyword` code search |
| `list_indexed_projects()` | List indexed repositories, file/chunk counts, and languages |
| `get_project_status(repo)` | Check index state and stale/new/deleted file counts |
| `remove_project(repo)` | Delete a repository and its indexed chunks |
| `health_check()` | Verify Ollama connectivity, model availability, and store stats |

## Environment Variables

| Variable | Default |
| :--- | :--- |
| `CODE_INDXR_DATA_DIR` | `%LOCALAPPDATA%\code-indxr` |
| `CODE_INDXR_OLLAMA_URL` | `http://127.0.0.1:11434` |
| `CODE_INDXR_EMBED_MODEL` | `nomic-embed-text` |
| `CODE_INDXR_BATCH_SIZE` | `32` |
| `CODE_INDXR_CONCURRENCY` | `2` |
| `CODE_INDXR_TIMEOUT` | `120` |
| `CODE_INDXR_MAX_FILE_BYTES` | `524288` |
| `CODE_INDXR_MAX_CHUNK_CHARS` | `2400` |
| `CODE_INDXR_CHUNK_OVERLAP_LINES` | `6` |
