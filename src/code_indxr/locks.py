from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import IO, Any

from code_indxr.models import RepoLockError

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class RepoLock:
    """Cross-process advisory file lock per repository using OS kernel locks."""

    def __init__(self, data_dir: Path, repo_id: str, timeout_seconds: float = 2.0) -> None:
        self.lock_dir = data_dir.resolve() / "locks"
        self.repo_id = repo_id
        self.lock_path = self.lock_dir / f"{repo_id}.lock"
        self.timeout_seconds = max(0.0, timeout_seconds)
        self._handle: IO[str] | None = None

    def _try_lock(self, handle: IO[str]) -> bool:
        if sys.platform == "win32":
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(self, handle: IO[str]) -> None:
        if sys.platform == "win32":
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass

    def _read_holder_info(self) -> str:
        try:
            content = self.lock_path.read_text(encoding="utf-8", errors="ignore").strip()
            return content or "unknown PID"
        except OSError:
            return "unknown PID"

    def acquire(self) -> None:
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + self.timeout_seconds
        try:
            while True:
                if self._try_lock(handle):
                    handle.seek(0)
                    handle.truncate()
                    handle.write(f"pid={os.getpid()}\n")
                    handle.flush()
                    self._handle = handle
                    return
                if time.monotonic() >= deadline:
                    holder = self._read_holder_info()
                    raise RepoLockError(
                        f"Repository '{self.repo_id}' is currently locked by another process ({holder}). "
                        f"Wait for it to finish and retry."
                    )
                time.sleep(0.05)
        except Exception:
            handle.close()
            raise

    def release(self) -> None:
        if self._handle is not None:
            try:
                self._unlock(self._handle)
            finally:
                self._handle.close()
                self._handle = None

    def __enter__(self) -> RepoLock:
        self.acquire()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.release()
