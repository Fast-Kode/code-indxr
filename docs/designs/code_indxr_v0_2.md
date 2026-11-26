# code-indxr v0.2: Deterministic Repo Context, Resumable CLI/MCP Indexing, and Public Release

*   **Author:** Samartha K V
*   **Status:** Proposed
*   **Date:** 2026-11-26

--------------------------------------------------------------------------------

## Overview

`code-indxr` is a local Python MCP server and CLI that indexes source repositories into an embedded hybrid vector and keyword search store using local Ollama embeddings (`nomic-embed-text`), `tree-sitter`, `LanceDB`, and `SQLite`. Zero source code leaves the user's machine.

Version `0.2.0` upgrades the current `0.1.0` single-process stdio prototype into a cross-platform (Windows, macOS, Linux), multi-client (Google Antigravity, Claude Code, VS Code / GitHub Copilot) developer tool ready for public GitHub release. It preserves already-indexed local repositories across upgrades, makes concurrent CLI and MCP access safe, adds batch-resumable indexing so large cold indexes survive timeouts, and introduces token-bounded deterministic repository orientation (directory tree, manifests, and symbol/signature outlines).

## The Problem

1. **Agents lack high-level orientation on unfamiliar repositories:** `search_code` returns isolated code chunks. An agent entering a new repository must guess search queries or run repeated directory listings to learn the project layout, build manifests, and available classes or functions.
2. **Cold indexing fails under MCP client timeouts:** Today `index_project` buffers all chunks and embeddings in memory and commits only at completion. On large repositories, an MCP client timeout (typically 60–120 seconds) or interruption discards all progress, and users have no terminal CLI subcommand (`code-indxr index`) to run long cold indexes directly.
3. **Concurrent CLI and MCP access corrupts or locks the local store:** `code-indxr` relies on an in-process `asyncio.Lock`, a single unprotected SQLite connection without `busy_timeout`, a process-local `_fts_ready` flag, and a thread-unsafe `tree-sitter` parser cache. Running a CLI index while an IDE MCP server is active risks `database is locked` errors, LanceDB commit conflicts, and torn state.
4. **Upgrades risk breaking existing local indexes:** `manifest.db` has no `PRAGMA user_version` schema versioning, `code_chunks` locks its Arrow vector dimension without recording `embed_model` or `vector_dim`, and `store.py` uses `LOWER(root_path)` matching that collides distinct paths on case-sensitive Linux filesystems.
5. **The repository is not ready for public GitHub users:** `README.md` hardcodes local Windows paths (`D:\Projects\...`) and Antigravity-only setup, `pyproject.toml` lacks public package metadata and a `LICENSE`, `code-indxr` hangs on `stdin` when run in a terminal, and there is no cross-platform CI.

## Goals

*   **G1 — Safe Upgrade & Cross-Process Concurrency:** Preserve existing `v0.1.0` indexed repositories via automatic `PRAGMA user_version` migrations with backups, fail fast with actionable messages on embedding model/dimension mismatches, and serialize concurrent CLI + multi-IDE MCP writes via OS-level per-repo file locks.
*   **G2 — Unified Cross-Platform CLI & Public Release:** Ship a single `code-indxr` CLI (`serve`, `index`, `search`, `map`, `outline`, `list`, `status`, `remove`, `doctor`) that prints help in an interactive TTY and starts stdio MCP when piped; support Windows, macOS, and Linux data directories; add `MIT` `LICENSE`, `CHANGELOG.md`, portable multi-client `README.md` (Antigravity, Claude Code, VS Code/Copilot), and 3-OS GitHub Actions CI.
*   **G3 — Batch-Resumable Indexing:** Commit chunked and embedded files in coarse batches to LanceDB and SQLite so interrupted MCP or CLI runs resume from the last committed batch without re-embedding completed files, while keeping MCP calls bounded against client timeouts.
*   **G4 — Deterministic Repo Orientation:** Provide `<20ms` token-bounded `get_repo_overview` and `get_symbol_outline` tools (directory tree, language counts, detected manifests, symbol signatures, imports, and optional docstrings) extracted during the existing Tree-sitter parse pass with zero extra LLM calls.

## Non-Goals

*   **No Git History Indexing:** Agents already run `git log` and `git blame` directly in milliseconds; `code-indxr` will not index or embed commit histories.
*   **No Local Chat-LLM Code Summarization:** Running a generative Ollama LLM alongside `nomic-embed-text` causes VRAM model-swap thrashing and latency spikes; indexing remains strictly deterministic.
*   **No Agent-Writable Context Notes:** Persistent notes duplicate native client memory (`GEMINI.md`, `CLAUDE.md`, `.github/copilot-instructions.md`, OpenViking) and would break the invariant that `code-indxr`'s data directory is a 100% disposable, regenerable cache.
*   **No Cloud Services or Telemetry:** No external network calls beyond the user's configured local Ollama endpoint.
*   **No Background File-Watcher Daemon:** Indexing remains explicit on-demand via MCP or CLI.
*   **No Automated IDE Config Mutation:** Users copy documented config snippets into Antigravity, Claude Code, or VS Code; `code-indxr` does not edit IDE config files.

## Open Questions

None. All scope and architectural decisions (`D1`–`D3`: canonical `repo` noun, TTY-aware bare CLI, `MIT` license) were resolved in `concepts_brief.md`.

## Critical User Journeys (CUJs)

### CUJ 1: Public User Onboarding (Zero to First Search)

*   **Given:** A developer on Windows, macOS, or Linux has Ollama running locally with `nomic-embed-text` pulled and wants local semantic code search in Antigravity, Claude Code, or VS Code/Copilot.
*   **When:** They install `code-indxr` via `uv tool install` / `pipx` / `pip`, run `code-indxr doctor` to verify Ollama and storage health, cold-index a repository with `code-indxr index /path/to/repo`, and paste the documented MCP snippet for their IDE.
*   **Then:** The IDE connects to `code-indxr` over stdio without protocol errors, and the agent can immediately query `get_repo_overview`, `get_symbol_outline`, and `search_code` with zero code leaving the machine.

### CUJ 2: Existing `v0.1.0` User Upgrading Without Data Loss

*   **Given:** A user already has repositories indexed in `%LOCALAPPDATA%\code-indxr` (or `~/.local/share/code-indxr`) from `v0.1.0` and an existing Antigravity `mcp_config.json` invoking `main.py` or bare `code-indxr`.
*   **When:** They update `code-indxr` to `v0.2.0` and start the MCP server or run `code-indxr status`.
*   **Then:** `manifest.db` is backed up and migrated automatically, existing `code_chunks` vectors remain queryable without forced re-embedding, deprecated `*project*` MCP tool aliases continue working, and bare `code-indxr` over piped stdio still starts the MCP server.

### CUJ 3: Resumable Cold Indexing Under Timeouts or Interruption

*   **Given:** A user or agent starts indexing a large repository (e.g., 3,000+ files) via `code-indxr index` or MCP `index_repo`.
*   **When:** The MCP call hits its per-call time budget (returning `status: "partial"`) or the user presses `Ctrl+C` in the terminal after 60% of files are processed.
*   **Then:** All previously committed batches remain persisted in LanceDB and SQLite; re-running `index_repo` or `code-indxr index` skips already-committed files and completes only the remaining files.

### CUJ 4: Fast Agent Orientation in an Unfamiliar Repository

*   **Given:** An agent in Antigravity, Claude Code, or VS Code opens a task in an indexed repository it has never seen before.
*   **When:** The agent calls `get_repo_overview` and `get_symbol_outline` before calling `search_code`.
*   **Then:** Within ~20ms and a strict token budget, the agent receives the bounded directory tree, detected build manifests, language breakdown, and top-level/filtered class and function signatures, enabling targeted `search_code` queries.

--------------------------------------------------------------------------------

## Technical Plan (Architecture)

### 1. System Architecture & Data Flow

`code-indxr` separates **stateless transport adapters** (`cli.py` for terminal usage, `server.py` for FastMCP stdio) from **shared domain services** (`indexer.py`, `scanner.py`, `chunker.py`, `embedder.py`, `store.py`, `locks.py`). Both adapters share one composition root (`app.py`) and coordinate writes against the same local `<data_dir>` using OS-level per-repo advisory locks and SQLite WAL transactions.

```text
+-----------------------+       +----------------------------------+
| Terminal CLI          |       | MCP Hosts (stdio JSON-RPC)       |
| code-indxr index/map  |       | Antigravity | Claude | VS Code   |
+-----------+-----------+       +----------------+-----------------+
            |                                    |
            v                                    v
     +------+------+                      +------+------+
     |   cli.py    |                      |  server.py  |  (stdout = JSON-RPC only;
     +------+------+                      +------+------+   logs -> stderr)
            |                                    |
            +-----------------+------------------+
                              |
                              v
                    +---------+---------+
                    |  app.py (wiring)  |
                    +---------+---------+
                              |
                              v
                    +---------+---------+
                    |    indexer.py     |  <-- RepoLock (<data_dir>/locks/<repo_id>.lock)
                    +----+----+----+----+
                         |    |    |
         +---------------+    |    +------------------+
         v                    v                       v
+--------+--------+  +--------+--------+     +--------+--------+
|   scanner.py    |  |   chunker.py    |     |   embedder.py   |
| .gitignore +    |  | Single-pass AST |     | Ollama async    |
| manifest filter |  | chunks+outlines |     | /api/embed      |
+-----------------+  +--------+--------+     +--------+--------+
                              |                       |
                              +-----------+-----------+
                                          |
                                          v
                              +-----------+-----------+
                              |       store.py        |
                              | SQLite (manifest.db)  |
                              | LanceDB (code_chunks) |
                              +-----------------------+
```

### 2. Cross-Process Concurrency & Thread Safety

1. **Per-Repo OS File Locks (`src/code_indxr/locks.py`):**
   - Replace the in-process `asyncio.Lock` in `CodeIndexer` with a cross-process advisory file lock (`msvcrt.locking` on Windows, `fcntl.flock` on POSIX) at `<data_dir>/locks/<repo_id>.lock`.
   - Kernel-managed locks auto-release if a CLI or MCP process crashes or receives `Ctrl+C`.
   - Writers acquire the lock with a short configurable timeout; if another process holds the lock, `index_repo` / `code-indxr index` fails fast with a structured `repo_locked` message naming the holder PID (best-effort metadata written inside the lockfile).
2. **Thread-Safe SQLite (`src/code_indxr/store.py`):**
   - Configure `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=10000`, `PRAGMA synchronous=NORMAL`, and `PRAGMA foreign_keys=ON`.
   - Wrap all SQLite connection operations in a `threading.Lock` and open write transactions with `BEGIN IMMEDIATE` so worker threads (`asyncio.to_thread`) and concurrent processes never interleave cursor state.
3. **Thread-Safe Tree-sitter Parsing (`src/code_indxr/chunker.py`):**
   - Replace `@functools.lru_cache` on mutable `tree_sitter.Parser` instances with cached immutable `Language` objects (`get_language`) and `threading.local()` parser instances.
4. **Cross-Process LanceDB & FTS State:**
   - Remove the process-local `self._fts_ready` boolean. Track FTS freshness via a persisted `fts_dirty` flag (and `store_generation` integer) in SQLite `store_meta`.
   - Open fresh LanceDB table references when `store_generation` advances so an MCP server sees CLI commits immediately, and wrap LanceDB `delete`/`add`/`create_index` in a bounded retry loop for transient commit conflicts.

### 3. Schema Migration & Model/Dimension Guard

1. **Numbered SQLite Migrations (`PRAGMA user_version`):**
   - `v1` represents the existing `v0.1.0` schema (`repos`, `files`).
   - Before applying any migration (`v1 -> v2`), copy `manifest.db` to `manifest.db.bak.v<N>` and run DDL inside a single `BEGIN IMMEDIATE` transaction.
   - If an existing `manifest.db` has `user_version` higher than the running binary supports, fail fast with a clear downgrade warning instead of mutating data.
2. **Additive `v2` SQLite Schema (Zero-Breakage for `v0.1.0` Repos):**
   - `store_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)`: stores `embed_model`, `vector_dim`, `fts_dirty`, `store_generation`. On `v1 -> v2` migration, backfill `vector_dim` by inspecting the existing `code_chunks` Arrow schema (if present) and default `embed_model` to the configured model.
   - `repo_manifests(repo_id, rel_path, manifest_kind, PRIMARY KEY (repo_id, rel_path))` with `ON DELETE CASCADE`: records detected root/subpackage build manifests (`pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, `pubspec.yaml`, `build.gradle*`, `pom.xml`, `*.csproj`, `*.sln`).
   - `file_symbols(repo_id, rel_path, symbol_name, symbol_kind, signature, docstring_excerpt, start_line, end_line)` and `file_imports(repo_id, rel_path, import_text)` with `ON DELETE CASCADE` and index `(repo_id, rel_path)`.
   - Keep the existing 11-column LanceDB `code_chunks` table schema untouched so `v0.1.0` vectors remain immediately searchable.
3. **Embedding Model & Dimension Guard:**
   - Before embedding or vector-querying, verify `(settings.embed_model, returned_dim)` against `store_meta`.
   - If the configured model or dimension differs from the existing store, fail fast with an actionable remediation message (`Embedding model changed (nomic-embed-text:768 -> ...); run 'code-indxr index <path> --force' or set a separate CODE_INDXR_DATA_DIR`).

### 4. Batch-Resumable Indexing Pipeline

```text
scan_repository (stat-first: check mtime_ns + size_bytes before 4KB binary sniff)
  -> diff against SQLite `files`
  -> partition changed files into batches (default: 50 files or ~500 chunks)
     for each batch:
       1. ThreadPool/to_thread: single-pass Tree-sitter -> CodeChunks + FileSymbols + FileImports
       2. OllamaEmbedder: async batched /api/embed for batch chunks
       3. LanceDB: delete replaced rel_paths + add batch chunks
       4. SQLite (BEGIN IMMEDIATE): upsert `files`, `file_symbols`, `file_imports`, `repo_manifests`, set `fts_dirty=1`, bump `store_generation`
       5. If MCP time budget exceeded (default 45s): commit current batch, return status="partial"
  -> end of run (when remaining == 0):
       rebuild LanceDB FTS index once -> set `fts_dirty=0` -> update `repos` summary counts
```

- **Crash/Timeout Recovery:** Because LanceDB writes precede SQLite `files` commits per batch, any crash or `Ctrl+C` leaves already-committed batches intact and skips them on the next run. If FTS is still marked `fts_dirty=1` after a partial run, `search_code` triggers a lazy FTS rebuild once or surfaces `degraded_modes: ["fts_pending"]` without failing.
- **MCP Bounded Execution:** `index_repo` accepts `time_budget_seconds` (default `45.0`, `0` for unbounded) and emits FastMCP `ctx.report_progress(...)` updates per batch. When the budget expires between batches, it returns `status: "partial"`, `files_indexed`, `files_remaining`, and `resume_hint`.

### 5. Deterministic Repo Overview & Symbol Outline

- **Single-Pass Extraction (`src/code_indxr/chunker.py`):** Extend Tree-sitter parsing into `extract_file_artifacts(...)`, returning `(chunks, symbols, imports)` in one parse.
  - Descend into class/interface/mixin bodies regardless of class size so normal-sized classes still expose their methods in `file_symbols`.
  - Extract `signature` as the collapsed single-line header (`node.start_byte` to `body.start_byte`, capped at 200 chars), plus first-line docstring/doc-comment excerpt (capped at 160 chars) where present.
  - Extract top-level `import` / `from ... import` / `package` / `use` statements into `file_imports`.
- **Token-Budgeted Retrieval:**
  - `get_repo_overview(repo, max_depth=3, max_entries=150)`: built purely from SQLite (`repos`, `files`, `repo_manifests`), returning language breakdown, detected manifest paths, bounded directory tree, `indexed_at`, and `truncated` flag in `<20ms`.
  - `get_symbol_outline(repo, path_prefix=None, kind=None, include_docstrings=False, max_symbols=150)`: queries `file_symbols` and `file_imports` in SQLite with `total_symbols` and `truncated` flags.

### 6. Unified CLI, MCP Surface & Cross-Platform Fixes

1. **Canonical Naming & Backward-Compatible Aliases (`D1`):**
   - MCP tools: `index_repo`, `search_code`, `get_repo_overview`, `get_symbol_outline`, `list_repos`, `get_repo_status`, `remove_repo`, `health_check`.
   - Keep `index_project`, `list_indexed_projects`, `get_project_status`, `remove_project` as thin deprecated aliases delegating to the `repo` tools so `v0.1.0` clients do not break.
   - Normalize common language aliases in `search_code` (`ts` -> `typescript`, `py` -> `python`, `js` -> `javascript`, `kt` -> `kotlin`, `md` -> `markdown`) and surface `degraded_modes` when FTS or Ollama is unavailable (allowing `keyword` fallback).
2. **TTY-Aware Entrypoint (`D2`):**
   - `pyproject.toml`: `code-indxr = "code_indxr.cli:main"`, plus `src/code_indxr/__main__.py` for `python -m code_indxr`.
   - Bare `code-indxr` checks `sys.stdin.isatty()`: prints CLI help when interactive; starts stdio MCP server (`serve`) when piped. `main.py` stays as a thin shim calling `server.main()` for existing `mcp_config.json` entries.
   - CLI subcommands: `serve`, `index <path> [--name] [--force] [--batch-size]`, `search <query> [--repo] [--lang] [--mode] [--top-k]`, `map <repo> [--depth] [--max-entries]`, `outline <repo> [--path-prefix] [--kind] [--docstrings]`, `list`, `status <repo> [--fast]`, `remove <repo>`, `doctor`.
3. **Cross-Platform & Observability Fixes:**
   - `config.py`: Use `LOCALAPPDATA` on Windows, `~/Library/Application Support/code-indxr` on macOS (falling back to `~/.local/share/code-indxr` if already populated), and `$XDG_DATA_HOME/code-indxr` (or `~/.local/share/code-indxr`) on Linux. Respect `OLLAMA_HOST` if `CODE_INDXR_OLLAMA_URL` is unset. Validate env vars with clear error messages.
   - `store.py`: Apply case-insensitive `root_path` matching only on Windows (`os.name == "nt"`), and escape `%` and `_` in `path_prefix` `LIKE` filters.
   - `scanner.py`: Keep `DEFAULT_IGNORED_DIRS` accurate (avoid dropping nested `src/env/` or `internal/build/` unintentionally by matching top-level/build patterns carefully or documenting exclusions) and detect manifest filenames even when their extension isn't code-indexed (e.g., `go.mod`, `Cargo.toml`, `*.csproj`, `*.sln`, `pubspec.yaml`).
   - Logging: Remove import-time `logging.basicConfig` from `server.py`; configure `stderr` logging at entrypoint startup (`CODE_INDXR_LOG_LEVEL`), and log swallowed FTS/parse/read failures.
4. **Public GitHub Packaging (`D3 = MIT`):**
   - Add `LICENSE` (MIT, copyright `Samartha K V`), `CHANGELOG.md`, complete `pyproject.toml` metadata (`license`, `readme`, `authors`, `keywords`, `classifiers`, `[project.urls]`), remove unused `pydantic` dependency, add `ruff` config, portable multi-client `README.md` + `examples/mcp/` (`antigravity.json`, `claude-code.md`, `vscode-copilot.json`), and `.github/workflows/ci.yml` (`ubuntu`, `macos`, `windows` × Python `3.11`, `3.12`, `3.13`).

--------------------------------------------------------------------------------

## Alternatives Considered

### Alternative A: Include Git History Indexing, Local Ollama LLM Summaries, and Agent-Writable Context Notes

*   **Pros:** Captures commit-level architectural rationale, prose summaries for undocumented code, and persistent cross-session agent notes inside one MCP server.
*   **Cons:** Duplicates native agent tools (`git log`, `CLAUDE.md`, `GEMINI.md`, `.github/copilot-instructions.md`, OpenViking), causes Ollama VRAM model-swap thrashing when interleaving `nomic-embed-text` with a 3B–8B chat model, slows indexing by 100x+, and turns a disposable cache into a stateful system of record where `remove_project` or schema resets risk data loss.
*   **Reason for Rejection:** Rejected by author: native agent tools already handle git history and persistent memory, and keeping `code-indxr` strictly deterministic preserves fast local indexing and the 100% disposable-cache invariant.

### Alternative B: Store Repo Overviews, Symbols, or New Metadata Inside the Existing `code_chunks` LanceDB Table

*   **Pros:** Keeps all indexed data in a single LanceDB table with one query path.
*   **Cons:** Breaks existing `v0.1.0` PyArrow schemas on `table.add()`, forces every existing local repository to re-embed from scratch on upgrade, and mixes structural metadata into vector similarity rankings where prose/headers displace raw code chunks.
*   **Reason for Rejection:** Rejected in favor of keeping the `code_chunks` LanceDB schema unchanged and storing deterministic `repo_manifests`, `file_symbols`, and `file_imports` in additive SQLite tables (`ON DELETE CASCADE`) for `<20ms` structured queries.

### Alternative C: Per-File Checkpointing with a Dedicated `index_jobs` State Machine

*   **Pros:** Granular single-file resume after interruption.
*   **Cons:** Flushing LanceDB and rebuilding the global FTS index every 1–10 files creates severe fragment proliferation, multiplies retained table versions on disk, and degrades indexing to O(N^2) time.
*   **Reason for Rejection:** Rejected in favor of reusing SQLite `files` as the natural checkpoint and committing in coarse 50-file / ~500-chunk batches with a single FTS rebuild at run completion.

### Alternative D: Require Explicit `code-indxr serve` (Breaking Bare `code-indxr`)

*   **Pros:** Unambiguous CLI behavior where running `code-indxr` always prints help.
*   **Cons:** Breaks any existing `v0.1.0` MCP client configuration that invokes bare `code-indxr` without arguments.
*   **Reason for Rejection:** Rejected by author (`D2`): bare `code-indxr` checks `sys.stdin.isatty()`, printing help in an interactive terminal while starting stdio MCP when piped, alongside explicit `code-indxr serve`.

### Alternative E: Rely on Prompt Instructions Telling Agents to Wait Indefinitely for Cold MCP Indexing

*   **Pros:** Zero code changes to `CodeIndexer.index_project`.
*   **Cons:** MCP client hosts (Antigravity, Claude Code, VS Code) enforce hard transport-layer tool timeouts (often 60–120 seconds) that prompt instructions cannot override, discarding in-memory progress on large repositories.
*   **Reason for Rejection:** Rejected in favor of coarse disk checkpointing, a bounded per-call MCP `time_budget_seconds` returning `status: "partial"`, and a terminal `code-indxr index` CLI command for unbounded cold runs.

--------------------------------------------------------------------------------

## Non-Functional Requirements

*   **Privacy & Offline Operation:** Zero telemetry and zero network calls outside the user's configured local Ollama endpoint (`CODE_INDXR_OLLAMA_URL` or `OLLAMA_HOST`).
*   **Performance & Latency:**
    *   `get_repo_overview` and `get_symbol_outline`: `<20ms` served from SQLite, with strict output caps (`max_entries=150`, `max_symbols=150`).
    *   Incremental `index_repo` with zero changed files: `<500ms` via `stat`-first skipping (no 4 KB file reads on unchanged `mtime_ns`/`size_bytes`).
    *   MCP `index_repo` time budget: default `45.0s` soft cutoff between batches so calls complete cleanly before client transport timeouts.
*   **Backward Compatibility & Rollback:**
    *   `v1 -> v2` SQLite migration creates `manifest.db.bak.v1` before altering `manifest.db` and never invalidates existing `code_chunks` embeddings.
    *   Deprecated `*project*` MCP tool names remain functional aliases for `v0.2.0`.
*   **Cross-Platform Support:** Verified on Windows, macOS, and Linux across Python `3.11`, `3.12`, and `3.13` with zero hardcoded local paths and strict `stdout` isolation in MCP `serve` mode.

--------------------------------------------------------------------------------

## Detailed Implementation Plan

### Target Files to Create/Modify

#### 1. `src/code_indxr/config.py` (Modify)
*   **Type / Scope:** Core Configuration
*   **Proposed Changes:** Replace import-time `data_dir` default with `field(default_factory=...)` supporting Windows (`%LOCALAPPDATA%\code-indxr`), macOS (`~/Library/Application Support/code-indxr`, falling back to `~/.local/share/code-indxr` if already populated), and Linux (`$XDG_DATA_HOME/code-indxr` or `~/.local/share/code-indxr`). Fall back to `OLLAMA_HOST` when `CODE_INDXR_OLLAMA_URL` is unset. Wire `CODE_INDXR_MIN_CHUNK_CHARS` and `CODE_INDXR_LOG_LEVEL` (default `WARNING`), add `batch_files` (`50`) and `mcp_time_budget_seconds` (`45.0`), and validate numeric env vars with clear error messages naming the offending variable.
*   **Rationale:** Prevents breaking existing installs on upgrade, fixes macOS/Linux path conventions, respects standard Ollama env vars, and avoids raw startup `ValueError` crashes.

#### 2. `src/code_indxr/models.py` (Modify)
*   **Type / Scope:** Domain Data Models
*   **Proposed Changes:** Add `SymbolInfo`, `FileOutline`, `RepoOverview`, and `RepoLockError` models. Extend `IndexStats` with `status` (`"complete" | "partial"`), `files_remaining`, `ast_fallback_files`, `unreadable_files`, `resume_hint`, and cap `errors` to 50 entries with `errors_truncated`. Extend `RepoStatus` with `schema_version`, `embed_model`, `vector_dim`, and `fts_dirty`. Keep all existing `to_dict()` keys intact.
*   **Rationale:** Gives MCP clients and CLI users explicit partial-index progress, degraded-mode diagnostics, and structured outline/overview payloads without breaking `v0.1.0` callers.

#### 3. `src/code_indxr/locks.py` (Create)
*   **Type / Scope:** Cross-Process Locking Utility
*   **Proposed Changes:** Implement `RepoLock(data_dir, repo_id, timeout_seconds=2.0)` using `msvcrt.locking` on Windows and `fcntl.flock` on POSIX against `<data_dir>/locks/<repo_id>.lock`, writing holder PID/timestamp metadata for diagnostics and releasing automatically on exit or crash.
*   **Rationale:** Prevents concurrent CLI and multi-IDE MCP server writes from corrupting SQLite or colliding in LanceDB.

#### 4. `src/code_indxr/scanner.py` (Modify)
*   **Type / Scope:** Repository File & Manifest Scanner
*   **Proposed Changes:** Add `MANIFEST_FILENAMES` detection (`pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, `pubspec.yaml`, `build.gradle`, `build.gradle.kts`, `pom.xml`, `*.csproj`, `*.sln`) independent of code-language filtering. Split `scan_repository()` into a `stat`-only metadata pass (deferring the 4 KB binary NUL-byte sniff until a file is actually selected for hashing/reading) and log unreadable `.gitignore` or source files via `logging`.
*   **Rationale:** Eliminates unnecessary disk reads on unchanged files (especially under Windows Defender) and ensures build manifests appear in `get_repo_overview`.

#### 5. `src/code_indxr/chunker.py` (Modify)
*   **Type / Scope:** Single-Pass Tree-sitter Chunker & Outline Extractor
*   **Proposed Changes:** Replace mutable `@lru_cache` `Parser` instances with cached immutable `Language` objects and `threading.local()` parsers. Add `extract_file_artifacts(repo_id, rel_path, content, language, ...)` returning `(chunks, symbols, imports, used_fallback)` in one AST parse: always descend into class/interface/mixin bodies for `symbols` (even when the class fits in one chunk), extract single-line `signature` (`node.start_byte` to `body.start_byte`, <=200 chars) + first-line docstring/comment excerpt (<=160 chars), and extract top-level imports. Keep `chunk_file(...)` as a thin wrapper for backward compatibility.
*   **Rationale:** Makes worker-thread parsing thread-safe and extracts full symbol outlines with zero extra disk reads or second parse passes.

#### 6. `src/code_indxr/embedder.py` (Modify)
*   **Type / Scope:** Async Ollama Client
*   **Proposed Changes:** Map `httpx.ConnectError` / `TimeoutException` into actionable `EmbedderError` messages (`Start Ollama with 'ollama serve' and verify CODE_INDXR_OLLAMA_URL`). Keep `EmbedderProtocol` clean and log retry attempts at `DEBUG`/`WARNING` to `stderr`.
*   **Rationale:** Replaces opaque `httpx` socket errors with clear remediation steps inside MCP/CLI outputs.

#### 7. `src/code_indxr/store.py` (Modify)
*   **Type / Scope:** SQLite Manifest + LanceDB Vector/FTS Store
*   **Proposed Changes:**
    - Configure SQLite `PRAGMA busy_timeout=10000`, `PRAGMA synchronous=NORMAL`, `threading.Lock` around connection access, and `BEGIN IMMEDIATE` for write transactions.
    - Add `PRAGMA user_version` migration runner (`v1 -> v2`) with automatic `manifest.db.bak.v1` backup before DDL, creating `store_meta`, `repo_manifests`, `file_symbols`, and `file_imports` (`ON DELETE CASCADE`).
    - Record and validate `(embed_model, vector_dim)` in `store_meta` (backfilling existing `v0.1.0` stores from `code_chunks` Arrow schema) and fail fast with rebuild instructions on mismatch.
    - Gate `LOWER(root_path)` matching to `os.name == "nt"` (fixing Linux case sensitivity) and escape `%`/`_` in `path_prefix` `LIKE` filters.
    - Replace process-local `_fts_ready` with persisted `store_meta` (`fts_dirty`, `store_generation`), reopen LanceDB table handles when `store_generation` changes, wrap LanceDB writes in transient conflict retries, and expose `get_repo_overview` / `get_symbol_outline` SQLite queries.
*   **Rationale:** Protects already-indexed `v0.1.0` repos, eliminates SQLite/LanceDB concurrency hazards, and powers `<20ms` deterministic orientation queries.

#### 8. `src/code_indxr/indexer.py` (Modify)
*   **Type / Scope:** Resumable Indexing & Search Orchestrator
*   **Proposed Changes:** Acquire `RepoLock` around mutating `index_repo` / `remove_repo` calls. Refactor `index_project` (aliased as `index_repo`) to process changed files in coarse batches (`batch_files`, default 50), running `extract_file_artifacts` via `asyncio.to_thread`, embedding batch chunks, writing LanceDB + SQLite `files`/`file_symbols`/`file_imports`/`repo_manifests`, and checking `time_budget_seconds` between batches to return `status: "partial"` cleanly when exceeded. Rebuild FTS once when `remaining == 0`. Add language alias normalization and `keyword` fallback (`degraded_modes`) in `search_code` when Ollama is unavailable. Add `get_repo_overview`, `get_symbol_outline`, and `fast` metadata-only `get_repo_status`.
*   **Rationale:** Makes cold indexing resumable across timeouts/interruptions and ensures `search_code` degrades gracefully when Ollama is offline.

#### 9. `src/code_indxr/app.py` (Create)
*   **Type / Scope:** Composition Root & Stderr Logging Setup
*   **Proposed Changes:** Centralize `configure_logging(level)` (strictly `sys.stderr`) and `build_indexer(settings)` factory used by both `cli.py` and `server.py`.
*   **Rationale:** Eliminates import-time `logging.basicConfig` side effects and shares one wiring path between CLI and MCP server.

#### 10. `src/code_indxr/server.py` (Modify)
*   **Type / Scope:** FastMCP Stdio Server Adapter
*   **Proposed Changes:** Expose canonical `repo` tools (`index_repo`, `search_code`, `get_repo_overview`, `get_symbol_outline`, `list_repos`, `get_repo_status`, `remove_repo`, `health_check`) plus deprecated `*project*` aliases (`index_project`, `list_indexed_projects`, `get_project_status`, `remove_project`). Wire FastMCP `ctx: Context` progress notifications in `index_repo`, keep `stdout` strictly for JSON-RPC, and update `SERVER_INSTRUCTIONS` with the orientation-first agent workflow (`get_repo_overview` -> `get_symbol_outline` -> `search_code`).
*   **Rationale:** Standardizes vocabulary on `repo`, supports progress reporting, and keeps `v0.1.0` clients working.

#### 11. `src/code_indxr/cli.py` and `src/code_indxr/__main__.py` (Create)
*   **Type / Scope:** Unified CLI Entrypoint
*   **Proposed Changes:** Implement stdlib `argparse` CLI (`serve`, `index`, `search`, `map`, `outline`, `list`, `status`, `remove`, `doctor`). When invoked with no subcommand, check `sys.stdin.isatty()`: print help if interactive TTY, or launch `serve` (stdio MCP) if piped. Add `doctor` command checking Ollama reachability, `nomic-embed-text` availability, SQLite `user_version`, LanceDB table health, and active locks.
*   **Rationale:** Fixes the terminal stdin hang while preserving zero-arg piped MCP launches and gives users a 1-command preflight diagnostic.

#### 12. `main.py` (Modify)
*   **Type / Scope:** Legacy Dev Shim
*   **Proposed Changes:** Delegate directly to `code_indxr.server:main` for existing local `mcp_config.json` setups.
*   **Rationale:** Preserves backward compatibility without appearing in public end-user docs.

#### 13. `pyproject.toml`, `LICENSE`, `CHANGELOG.md`, `README.md`, `examples/mcp/*`, `.github/workflows/ci.yml` (Create / Modify)
*   **Type / Scope:** Public OSS Packaging, Documentation & CI
*   **Proposed Changes:**
    - `pyproject.toml`: Bind `code-indxr = "code_indxr.cli:main"`, add `license = "MIT"`, `readme`, `authors`, `keywords`, `classifiers`, `[project.urls]`, remove unused `pydantic`, add `ruff` dev dependency and config.
    - `LICENSE`: MIT License (`Copyright (c) 2026 Samartha K V`).
    - `CHANGELOG.md`: Document `v0.1.0` and `v0.2.0` changes.
    - `README.md` & `examples/mcp/` (`antigravity.json`, `claude-code.md`, `vscode-copilot.json`): Remove hardcoded personal Windows paths, document `uv tool install`/`pipx`/`pip`, `code-indxr doctor`, OS-specific data dirs, env vars with units, and copy-pasteable configs for Antigravity, Claude Code, and VS Code/Copilot.
    - `.github/workflows/ci.yml`: Run `ruff check`, `ruff format --check`, and `pytest` across `ubuntu-latest`, `macos-latest`, and `windows-latest` on Python `3.11`, `3.12`, and `3.13`.
*   **Rationale:** Meets the minimum bar for a clean, portable, verified public GitHub release.

#### 14. `tests/` (`test_migration.py`, `test_locks.py`, `test_cli.py`, `test_repomap.py`, updates to `test_indexer.py`, `test_server.py`, `test_chunker.py`, `test_store.py`) (Create / Modify)
*   **Type / Scope:** Automated Test Suite
*   **Proposed Changes:** Add tests for `v1 -> v2` SQLite migration + backup from a real `v0.1.0` fixture DB, `embed_model`/`vector_dim` mismatch errors, cross-process `RepoLock` contention, batch-resumable `partial` indexing + crash resume, single-pass `get_repo_overview` & `get_symbol_outline`, CLI subcommands + TTY vs piped dispatch + `doctor`, and deprecated `*project*` MCP aliases.
*   **Rationale:** Guarantees regressions, migration breaks, and concurrency races are caught offline in CI.

### Verification Plan

*   **Unit & Integration Tests:** `.\.venv\Scripts\python.exe -m pytest -v` (all mocked Ollama + real SQLite/LanceDB/Tree-sitter tests passing across Windows, macOS, and Linux in CI).
*   **Static Analysis & Formatting:** `ruff check .` and `ruff format --check .`
*   **Package Build Check:** `python -m build` (or `pip install --no-build-isolation -e .`) and `code-indxr --help` + `code-indxr doctor`.
*   **End-to-End MCP & Live Ollama Check:** Piped stdio MCP handshake (`code-indxr serve` and bare piped `code-indxr`), live `index_repo` + `get_repo_overview` + `get_symbol_outline` + `search_code` against local Ollama `nomic-embed-text`.

--------------------------------------------------------------------------------

*Drafted with EGM*
