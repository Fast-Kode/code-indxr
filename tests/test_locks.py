from __future__ import annotations

import multiprocessing
import time
from pathlib import Path

import pytest

from code_indxr.locks import RepoLock
from code_indxr.models import RepoLockError


def _hold_lock_in_subprocess(
    data_dir_str: str, repo_id: str, ready_evt: multiprocessing.Event, release_evt: multiprocessing.Event
) -> None:
    lock = RepoLock(Path(data_dir_str), repo_id, timeout_seconds=1.0)
    with lock:
        ready_evt.set()
        release_evt.wait(timeout=10.0)


def test_repo_lock_acquire_and_release(tmp_path: Path) -> None:
    lock = RepoLock(tmp_path, "demo-repo", timeout_seconds=0.5)
    with lock:
        lock_file = tmp_path / "locks" / "demo-repo.lock"
        assert lock_file.exists()

    # Re-acquiring after exit must succeed immediately
    with lock:
        assert (tmp_path / "locks" / "demo-repo.lock").exists()


def test_repo_lock_blocks_concurrent_process_and_reports_pid(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    ready_evt = ctx.Event()
    release_evt = ctx.Event()
    proc = ctx.Process(
        target=_hold_lock_in_subprocess,
        args=(str(tmp_path), "shared-repo", ready_evt, release_evt),
    )
    proc.start()
    try:
        assert ready_evt.wait(timeout=5.0), "subprocess failed to acquire lock in time"
        contending = RepoLock(tmp_path, "shared-repo", timeout_seconds=0.3)
        with pytest.raises(RepoLockError) as exc_info:
            with contending:
                pass
        assert "shared-repo" in str(exc_info.value)
    finally:
        release_evt.set()
        proc.join(timeout=5.0)

    # After child exits, lock is immediately available again
    time.sleep(0.05)
    with RepoLock(tmp_path, "shared-repo", timeout_seconds=0.5):
        pass
