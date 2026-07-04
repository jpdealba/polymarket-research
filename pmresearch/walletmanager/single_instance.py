"""Single-instance guard for the collector.

`pmr run` is a single-writer process against the SQLite ledger. Two of them
running at once fight over the write lock (observed as sporadic
``database is locked`` failures in enrichment/derive). This module provides a
cross-platform advisory file lock so a second ``pmr run`` refuses to start
instead of contending. The lock is held by an open file handle for the life of
the process, so the OS releases it automatically on exit *or crash* — there is
no stale-lock file to clean up.
"""

from __future__ import annotations

import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    """Raised when another process already holds the collector lock."""


def _try_lock(fh) -> None:
    """Take an exclusive, non-blocking lock on byte 0 of ``fh``.

    Raises OSError if another handle (this process or another) already holds it.
    """
    fh.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fh) -> None:
    fh.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class SingleInstanceLock:
    """Advisory whole-process lock backed by a lock file.

    Use as a context manager or call ``acquire()``/``release()`` directly.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # "a+" never truncates, so a file another process has byte-locked is
        # opened without disturbing it; we only truncate after we own the lock.
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            _try_lock(fh)
        except OSError as exc:
            fh.close()
            raise AlreadyRunning(
                f"another collector already holds {self.path}"
            ) from exc
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            _unlock(self._fh)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
