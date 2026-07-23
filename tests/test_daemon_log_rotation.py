from __future__ import annotations

import os
import threading
from pathlib import Path
from unittest.mock import patch

from trade_compass_agent.daemon import log_rotation


def test_rotate_file_skips_missing_small_and_non_regular_paths(tmp_path: Path) -> None:
    missing = tmp_path / "missing.log"
    small = tmp_path / "small.log"
    directory = tmp_path / "directory.log"
    small.write_bytes(b"1234")
    directory.mkdir()

    assert log_rotation.rotate_file(missing, max_bytes=4) is False
    assert log_rotation.rotate_file(small, max_bytes=4) is False
    assert log_rotation.rotate_file(directory, max_bytes=4) is False


def test_rotate_file_copy_truncates_and_preserves_generations(tmp_path: Path) -> None:
    path = tmp_path / "serve.stdout.log"
    path.write_bytes(b"first-generation")

    assert log_rotation.rotate_file(path, max_bytes=4, backup_count=2) is True
    assert path.read_bytes() == b""
    assert (tmp_path / "serve.stdout.log.1").read_bytes() == b"first-generation"

    path.write_bytes(b"second-generation")
    assert log_rotation.rotate_file(path, max_bytes=4, backup_count=2) is True
    assert path.read_bytes() == b""
    assert (tmp_path / "serve.stdout.log.1").read_bytes() == b"second-generation"
    assert (tmp_path / "serve.stdout.log.2").read_bytes() == b"first-generation"

    path.write_bytes(b"third-generation")
    assert log_rotation.rotate_file(path, max_bytes=4, backup_count=2) is True
    assert (tmp_path / "serve.stdout.log.1").read_bytes() == b"third-generation"
    assert (tmp_path / "serve.stdout.log.2").read_bytes() == b"second-generation"
    assert not (tmp_path / "serve.stdout.log.3").exists()


def test_rotate_logs_once_isolates_file_errors(tmp_path: Path) -> None:
    healthy = tmp_path / "healthy.log"
    healthy.write_bytes(b"large")

    with patch(
        "trade_compass_agent.daemon.log_rotation.rotate_file",
        side_effect=[OSError("denied"), True],
    ) as rotate:
        count = log_rotation.rotate_logs_once(
            (tmp_path / "broken.log", healthy),
            max_bytes=4,
        )

    assert count == 1
    assert rotate.call_count == 2


def test_launchd_rotation_only_starts_for_marked_macos_service(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(log_rotation.sys, "platform", "darwin")
    monkeypatch.setenv("TRADE_COMPASS_SERVICE_MARKER", "1")
    monkeypatch.setattr(
        "trade_compass_agent.daemon.launchd.log_dir",
        lambda: tmp_path,
    )

    with patch(
        "trade_compass_agent.daemon.log_rotation.start_log_rotation",
        return_value=object(),
    ) as start:
        worker = log_rotation.start_launchd_log_rotation()

    assert worker is not None
    assert start.call_args.args[0] == (
        tmp_path / "serve.stdout.log",
        tmp_path / "serve.stderr.log",
    )

    monkeypatch.setattr(log_rotation.sys, "platform", "linux")
    with patch("trade_compass_agent.daemon.log_rotation.start_log_rotation") as start:
        assert log_rotation.start_launchd_log_rotation() is None
    start.assert_not_called()


def test_rotation_worker_stops_without_touching_service_lifecycle(tmp_path: Path) -> None:
    stopped = threading.Event()
    stopped.set()

    with patch("trade_compass_agent.daemon.log_rotation.rotate_logs_once") as rotate:
        worker = log_rotation.start_log_rotation(
            (tmp_path / "serve.log",),
            check_interval=0.01,
            stop_event=stopped,
        )
        worker.join(timeout=1)

    assert not worker.is_alive()
    rotate.assert_not_called()


def test_launchd_rotation_setup_failure_does_not_block_service(
    monkeypatch,
) -> None:
    monkeypatch.setattr(log_rotation.sys, "platform", "darwin")
    monkeypatch.setenv("TRADE_COMPASS_SERVICE_MARKER", "1")

    with patch(
        "trade_compass_agent.daemon.launchd.log_dir",
        side_effect=OSError("read-only filesystem"),
    ):
        assert log_rotation.start_launchd_log_rotation() is None


def test_service_log_archives_keep_owner_only_mode(tmp_path: Path) -> None:
    path = tmp_path / "serve.stderr.log"
    path.write_bytes(b"secret diagnostic output")
    path.chmod(0o600)

    assert log_rotation.rotate_file(path, max_bytes=4) is True
    archive_mode = os.stat(tmp_path / "serve.stderr.log.1").st_mode & 0o777
    assert archive_mode == 0o600
