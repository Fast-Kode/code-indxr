# Contributing to code-indxr

Thanks for your interest in contributing to `code-indxr`!

## Local Development Setup

```bash
git clone https://github.com/Fast-Kode/code-indxr.git
cd code-indxr
python -m venv .venv
```

Install editable with dev dependencies:

- **Windows (PowerShell):**
  ```powershell
  .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
  ```
- **macOS / Linux:**
  ```bash
  ./.venv/bin/python -m pip install -e ".[dev]"
  ```

## Quality Checks

Run tests, linting, and formatting checks before opening a pull request:

```bash
pytest -v
ruff check .
ruff format --check .
```

All tests run offline using mocked Ollama HTTP responses and local temporary SQLite/LanceDB stores.

## Pull Request Guidelines

1. Fork the repository and create a focused branch (e.g., `feat/go-outline` or `fix/windows-lock`).
2. Keep changes small and well-scoped, with unit/integration tests covering new behavior or bug fixes.
3. Update `README.md` or `CHANGELOG.md` if you change CLI flags, MCP tools, or environment variables.
4. Keep `stdout` strictly clean in `serve` mode (MCP JSON-RPC framing); log only to `stderr`.
