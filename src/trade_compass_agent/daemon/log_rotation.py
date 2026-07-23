from __future__ import annotations

import logging
import os
import stat
import sys
import tempfile
import threading
from collections.abc import Iterable
from pathlib import Path


LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5
DEFAULT_CHECK_INTERVAL = 60.0


def rotate_file(
    path: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> bool:
    """Copy and truncate a launchd-owned log while preserving its open descriptor."""
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    if backup_count < 1:
        raise ValueError("backup_count must be positive")

    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_size <= max_bytes:
        return False

    try:
        source = path.open("r+b")
    except FileNotFoundError:
        return False

    temporary_path: Path | None = None
    with source:
        source_stat = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(source_stat.st_mode)
            or source_stat.st_dev != path_stat.st_dev
            or source_stat.st_ino != path_stat.st_ino
            or source_stat.st_size <= max_bytes
        ):
            return False

        with tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=f".{path.name}.rotate-",
            dir=path.parent,
            delete=False,
        ) as archive:
            temporary_path = Path(archive.name)
            remaining = source_stat.st_size
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                archive.write(chunk)
                remaining -= len(chunk)
            archive.flush()
            os.fsync(archive.fileno())

        try:
            oldest = _backup_path(path, backup_count)
            oldest.unlink(missing_ok=True)
            for index in range(backup_count - 1, 0, -1):
                previous = _backup_path(path, index)
                if previous.is_file():
                    os.replace(previous, _backup_path(path, index + 1))
            os.replace(temporary_path, _backup_path(path, 1))
            temporary_path = None

            source.seek(0)
            source.truncate(0)
            source.flush()
            os.fsync(source.fileno())
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    return True


def rotate_logs_once(
    paths: Iterable[Path],
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> int:
    rotated = 0
    for path in paths:
        try:
            rotated += rotate_file(
                path,
                max_bytes=max_bytes,
                backup_count=backup_count,
            )
        except Exception:
            LOGGER.warning("Could not rotate service log %s", path, exc_info=True)
    return rotated


def start_log_rotation(
    paths: Iterable[Path],
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    check_interval: float = DEFAULT_CHECK_INTERVAL,
    stop_event: threading.Event | None = None,
) -> threading.Thread:
    """Start a best-effort daemon worker for launchd log retention."""
    if check_interval <= 0:
        raise ValueError("check_interval must be positive")
    log_paths = tuple(paths)
    stopped = stop_event or threading.Event()

    def run() -> None:
        while not stopped.is_set():
            rotate_logs_once(
                log_paths,
                max_bytes=max_bytes,
                backup_count=backup_count,
            )
            stopped.wait(check_interval)

    worker = threading.Thread(
        target=run,
        name="trade-compass-log-rotation",
        daemon=True,
    )
    worker.start()
    return worker


def start_launchd_log_rotation() -> threading.Thread | None:
    """Enable file rotation only inside a macOS launchd-managed service."""
    if sys.platform != "darwin" or os.getenv("TRADE_COMPASS_SERVICE_MARKER") != "1":
        return None

    from trade_compass_agent.daemon.launchd import log_dir

    try:
        logs = log_dir()
        return start_log_rotation(
            (logs / "serve.stdout.log", logs / "serve.stderr.log")
        )
    except Exception:
        LOGGER.warning("Could not start launchd log rotation", exc_info=True)
        return None


def _backup_path(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")
