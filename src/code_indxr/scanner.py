from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

import pathspec

from code_indxr.models import DetectedManifest, ScannedFile, ScanResult

logger = logging.getLogger("code_indxr.scanner")

EXTENSION_LANGUAGE_MAP: dict[str, str] = {
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".py": "python",
    ".pyi": "python",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".java": "java",
    ".dart": "dart",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".sql": "sql",
    ".graphql": "graphql",
    ".gql": "graphql",
    ".proto": "proto",
    ".md": "markdown",
    ".mdx": "markdown",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".xml": "xml",
    ".gradle": "groovy",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".sh": "bash",
    ".bash": "bash",
    ".ps1": "powershell",
}

SPECIAL_FILENAMES: dict[str, str] = {
    "dockerfile": "dockerfile",
    "makefile": "makefile",
    "cmakelists.txt": "cmake",
}

MANIFEST_FILENAMES: dict[str, str] = {
    "pyproject.toml": "python",
    "setup.py": "python",
    "requirements.txt": "python",
    "package.json": "node",
    "tsconfig.json": "typescript",
    "cargo.toml": "rust",
    "go.mod": "go",
    "pubspec.yaml": "dart",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    "settings.gradle": "gradle",
    "settings.gradle.kts": "gradle",
    "pom.xml": "maven",
    "gemfile": "ruby",
    "podfile": "cocoapods",
    "dockerfile": "docker",
    "docker-compose.yml": "docker",
    "docker-compose.yaml": "docker",
}

MANIFEST_SUFFIXES: dict[str, str] = {
    ".csproj": "dotnet",
    ".sln": "dotnet",
}

DEFAULT_IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".turbo",
        ".cache",
        ".parcel-cache",
        "dist",
        "build",
        "out",
        "coverage",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".dart_tool",
        ".pub-cache",
        ".fvm",
        ".gradle",
        ".idea",
        ".vscode",
        "target",
        "Pods",
        ".terraform",
        "vendor",
        ".expo",
        ".angular",
    }
)

DEFAULT_IGNORED_FILES: frozenset[str] = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "bun.lock",
        "pubspec.lock",
        "poetry.lock",
        "uv.lock",
        "pipfile.lock",
        "cargo.lock",
        "composer.lock",
        "gemfile.lock",
        "gradle.lockfile",
        "podfile.lock",
    }
)

IGNORED_SUFFIXES: tuple[str, ...] = (
    ".min.js",
    ".min.css",
    ".bundle.js",
    ".chunk.js",
    ".map",
    ".g.dart",
    ".freezed.dart",
    ".mocks.dart",
)


def detect_language(path: Path) -> str | None:
    """Return canonical language identifier for a source file, or None if unsupported."""
    name_lower = path.name.lower()
    if name_lower in DEFAULT_IGNORED_FILES:
        return None
    if any(name_lower.endswith(suffix) for suffix in IGNORED_SUFFIXES):
        return None
    if name_lower in SPECIAL_FILENAMES:
        return SPECIAL_FILENAMES[name_lower]
    return EXTENSION_LANGUAGE_MAP.get(path.suffix.lower())


def detect_manifest_kind(path: Path) -> str | None:
    """Return manifest ecosystem kind if the file is a build/project manifest."""
    name_lower = path.name.lower()
    if name_lower in MANIFEST_FILENAMES:
        return MANIFEST_FILENAMES[name_lower]
    return MANIFEST_SUFFIXES.get(path.suffix.lower())


def is_binary_content(sample: bytes) -> bool:
    """Detect binary files by checking for NUL bytes in a leading sample."""
    return b"\x00" in sample


def is_binary_file(path: Path) -> bool:
    """Read the leading 4 KB of a file and check for binary NUL bytes."""
    with path.open("rb") as handle:
        sample = handle.read(4096)
    return is_binary_content(sample)


def compute_file_hash(path: Path) -> str:
    """Compute a fast, deterministic blake2b content hash for incremental change detection."""
    hasher = hashlib.blake2b(digest_size=16)
    with path.open("rb") as handle:
        while chunk := handle.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def _load_gitignore(directory: Path) -> pathspec.PathSpec | None:
    gitignore_path = directory / ".gitignore"
    if not gitignore_path.is_file():
        return None
    try:
        lines = gitignore_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError as exc:
        logger.warning("Failed to read .gitignore at %s: %s", gitignore_path, exc)
        return None
    return pathspec.PathSpec.from_lines("gitignore", lines)


def _is_ignored(
    rel_posix: str,
    is_dir: bool,
    specs: list[tuple[str, pathspec.PathSpec]],
) -> bool:
    for base_posix, spec in specs:
        if base_posix:
            prefix = f"{base_posix}/"
            if not rel_posix.startswith(prefix):
                continue
            target = rel_posix[len(prefix) :]
        else:
            target = rel_posix
        check = f"{target}/" if is_dir else target
        if spec.match_file(check):
            return True
    return False


def scan_repository_with_manifests(
    root_path: Path,
    max_file_bytes: int = 512 * 1024,
    *,
    sniff_binary: bool = True,
) -> ScanResult:
    """Walk a repository respecting .gitignore and default ignore rules, detecting manifests."""
    resolved_root = root_path.resolve()
    if not resolved_root.is_dir():
        raise FileNotFoundError(f"Repository path does not exist or is not a directory: {resolved_root}")

    specs_by_dir: dict[Path, list[tuple[str, pathspec.PathSpec]]] = {}
    root_spec = _load_gitignore(resolved_root)
    specs_by_dir[resolved_root] = [("", root_spec)] if root_spec else []

    files: list[ScannedFile] = []
    manifests: list[DetectedManifest] = []
    unreadable = 0

    for dirpath_str, dirnames, filenames in os.walk(resolved_root, topdown=True):
        current_dir = Path(dirpath_str)
        active_specs = list(specs_by_dir.get(current_dir, []))
        if current_dir != resolved_root:
            local_spec = _load_gitignore(current_dir)
            if local_spec:
                rel_dir_posix = current_dir.relative_to(resolved_root).as_posix()
                active_specs.append((rel_dir_posix, local_spec))

        kept_dirs: list[str] = []
        for dirname in sorted(dirnames):
            if dirname in DEFAULT_IGNORED_DIRS:
                continue
            child_dir = current_dir / dirname
            rel_dir = child_dir.relative_to(resolved_root).as_posix()
            if _is_ignored(rel_dir, True, active_specs):
                continue
            kept_dirs.append(dirname)
            specs_by_dir[child_dir] = active_specs
        dirnames[:] = kept_dirs

        for filename in sorted(filenames):
            file_path = current_dir / filename
            rel_posix = file_path.relative_to(resolved_root).as_posix()

            manifest_kind = detect_manifest_kind(file_path)
            if manifest_kind is not None and not _is_ignored(rel_posix, False, active_specs):
                manifests.append(DetectedManifest(rel_path=rel_posix, kind=manifest_kind))

            language = detect_language(file_path)
            if language is None:
                continue

            if _is_ignored(rel_posix, False, active_specs):
                continue

            try:
                stat = file_path.stat()
            except OSError as exc:
                unreadable += 1
                logger.warning("Failed to stat %s: %s", file_path, exc)
                continue

            if stat.st_size == 0 or stat.st_size > max_file_bytes:
                continue

            if sniff_binary:
                try:
                    if is_binary_file(file_path):
                        continue
                except OSError as exc:
                    unreadable += 1
                    logger.warning("Failed to sniff %s: %s", file_path, exc)
                    continue

            files.append(
                ScannedFile(
                    rel_path=rel_posix,
                    abs_path=file_path,
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    language=language,
                )
            )

    return ScanResult(files=files, manifests=manifests, unreadable_files=unreadable)


def scan_repository(root_path: Path, max_file_bytes: int = 512 * 1024) -> list[ScannedFile]:
    """Backward-compatible wrapper returning scanned source files."""
    return scan_repository_with_manifests(root_path, max_file_bytes=max_file_bytes, sniff_binary=True).files
