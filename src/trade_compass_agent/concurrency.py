"""Concurrency utilities for thread-safe file store operations.

Pattern: per-path threading.Lock + atomic file replace for RMW operations.
Follows CooldownTracker's established pattern (risk/cooldown.py).
"""

from __future__ import annotations

import os
import tempfile
import threading
from contextlib import contextmanager

import fcntl
from pathlib import Path

_path_locks: dict[str, threading.Lock] = {}
_registry_lock = threading.Lock()


def get_path_lock(path: Path) -> threading.Lock:
    """Get or create a per-path lock for thread-safe file operations."""
    key = str(path.resolve())
    if key not in _path_locks:
        with _registry_lock:
            if key not in _path_locks:
                _path_locks[key] = threading.Lock()
    return _path_locks[key]


def atomic_write(path: Path, content: str) -> None:
    """Write content to file atomically via tempfile + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        fd = -1
        os.replace(tmp_path, str(path))
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


_transaction_depth = threading.local()


@contextmanager
def file_transaction(path: Path):
    """Serialize a store across threads, instances and processes; allow nesting.

    The lock file is permanent: replacing/unlinking it would split the lock domain.
    """
    key = str(path.resolve())
    depths = getattr(_transaction_depth, "paths", None)
    if depths is None:
        depths = _transaction_depth.paths = {}
    if depths.get(key, 0):
        depths[key] += 1
        try:
            yield
        finally:
            depths[key] -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with get_path_lock(path), path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        depths[key] = 1
        try:
            yield
        finally:
            depths.pop(key, None)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
