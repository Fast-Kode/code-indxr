from __future__ import annotations

from pathlib import Path

from code_indxr.scanner import compute_file_hash, detect_language, is_binary_content, scan_repository


def test_detect_language_covers_primary_stacks() -> None:
    assert detect_language(Path("App.tsx")) == "tsx"
    assert detect_language(Path("utils.ts")) == "typescript"
    assert detect_language(Path("index.jsx")) == "javascript"
    assert detect_language(Path("main.py")) == "python"
    assert detect_language(Path("MainActivity.kt")) == "kotlin"
    assert detect_language(Path("build.gradle.kts")) == "kotlin"
    assert detect_language(Path("Service.java")) == "java"
    assert detect_language(Path("home_screen.dart")) == "dart"
    assert detect_language(Path("README.md")) == "markdown"
    assert detect_language(Path("image.png")) is None


def test_is_binary_content() -> None:
    assert is_binary_content(b"hello world\n") is False
    assert is_binary_content(b"\x89PNG\r\n\x1a\n\x00\x00\x00") is True


def test_scan_repository_respects_gitignore_and_default_ignores(sample_repo: Path) -> None:
    # Add a binary file and an oversized file
    (sample_repo / "backend" / "blob.py").write_bytes(b"\x00\x01\x02binary")
    (sample_repo / "backend" / "huge.py").write_text("x = 1\n" * 5000, encoding="utf-8")

    files = scan_repository(sample_repo, max_file_bytes=4096)
    rel_paths = {f.rel_path for f in files}

    assert "src/components/AuthCard.tsx" in rel_paths
    assert "backend/token_service.py" in rel_paths
    assert "android/app/SessionManager.kt" in rel_paths
    assert "android/app/CryptoHelper.java" in rel_paths
    assert "lib/profile_widget.dart" in rel_paths

    # Excluded by .gitignore, default ignores, lockfiles, binary sniff, or size limit
    assert "ignored_dir/secret.py" not in rel_paths
    assert "debug.log" not in rel_paths
    assert "node_modules/pkg/index.js" not in rel_paths
    assert "package-lock.json" not in rel_paths
    assert "backend/blob.py" not in rel_paths
    assert "backend/huge.py" not in rel_paths


def test_compute_file_hash_is_stable_and_detects_changes(tmp_path: Path) -> None:
    target = tmp_path / "mod.py"
    target.write_text("a = 1\n", encoding="utf-8")
    h1 = compute_file_hash(target)
    h2 = compute_file_hash(target)
    assert h1 == h2

    target.write_text("a = 2\n", encoding="utf-8")
    h3 = compute_file_hash(target)
    assert h3 != h1
