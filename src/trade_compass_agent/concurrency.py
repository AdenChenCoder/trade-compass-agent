"""Concurrency utilities for thread-safe file store operations.

Pattern: per-path threading.Lock + atomic file replace for RMW operations.
Follows CooldownTracker's established pattern (risk/cooldown.py).
"""

from __future__ import annotations

import os
import tempfile
import threading
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
