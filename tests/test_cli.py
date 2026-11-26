from __future__ import annotations

import json
from pathlib import Path

from code_indxr.cli import main as cli_main
from tests.conftest import FakeEmbedder


def test_cli_bare_tty_prints_help_and_exits_zero(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    exit_code = cli_main([])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "code-indxr" in captured.out
    assert "serve" in captured.out
    assert "index" in captured.out


def test_cli_bare_piped_invokes_serve(monkeypatch) -> None:
    called = {"serve": False}
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("code_indxr.cli._run_serve", lambda: called.__setitem__("serve", True) or 0)
    exit_code = cli_main([])
    assert exit_code == 0
    assert called["serve"] is True


def test_cli_index_map_outline_search_status_remove(
    sample_repo: Path,
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    data_dir = tmp_path / "cli-data"
    monkeypatch.setenv("CODE_INDXR_DATA_DIR", str(data_dir))
    monkeypatch.setattr(
        "code_indxr.app.OllamaEmbedder",
        lambda **kwargs: FakeEmbedder(dim=32),
    )

    assert cli_main(["index", str(sample_repo), "--json"]) == 0
    idx_out = json.loads(capsys.readouterr().out)
    assert idx_out["status"] == "complete"
    repo_id = idx_out["repo_id"]

    assert cli_main(["list", "--json"]) == 0
    list_out = json.loads(capsys.readouterr().out)
    assert list_out["count"] == 1

    assert cli_main(["map", repo_id, "--json"]) == 0
    map_out = json.loads(capsys.readouterr().out)
    assert map_out["repo_id"] == repo_id

    assert cli_main(["outline", repo_id, "--path-prefix", "backend/", "--json"]) == 0
    outline_out = json.loads(capsys.readouterr().out)
    assert outline_out["total_symbols"] >= 1

    assert cli_main(["search", "refresh_access_token", "--repo", repo_id, "--json"]) == 0
    search_out = json.loads(capsys.readouterr().out)
    assert search_out["count"] >= 1

    assert cli_main(["status", repo_id, "--fast", "--json"]) == 0
    status_out = json.loads(capsys.readouterr().out)
    assert status_out["indexed"] is True

    assert cli_main(["doctor", "--json"]) == 0
    doc_out = json.loads(capsys.readouterr().out)
    assert doc_out["ok"] is True

    assert cli_main(["remove", repo_id, "--json"]) == 0
    rm_out = json.loads(capsys.readouterr().out)
    assert rm_out["removed"] is True
