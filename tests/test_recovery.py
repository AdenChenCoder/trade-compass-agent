from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from trade_compass_agent.recovery import (
    BackupValidationError,
    RecoveryError,
    RecoveryLayout,
    create_backup,
    current_recovery_layout,
    inspect_backup,
    plan_restore,
    restore_backup,
)


def _layout(tmp_path: Path) -> RecoveryLayout:
    root = tmp_path / "runtime"
    return RecoveryLayout(
        config_path=root / "config.yaml",
        env_path=root / ".env",
        mcp_path=root / "mcp.json",
        data_dir=root / "data",
        memory_dir=root / "memory",
        backup_dir=tmp_path / "backups",
    )


def _seed(layout: RecoveryLayout) -> None:
    layout.config_path.parent.mkdir(parents=True)
    layout.config_path.write_text("profile: local\n", encoding="utf-8")
    layout.env_path.write_text("SECRET=test-only\n", encoding="utf-8")
    layout.mcp_path.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    layout.data_dir.mkdir()
    layout.memory_dir.mkdir()
    (layout.data_dir / "audit.jsonl").write_text('{"event": 1}\n', encoding="utf-8")
    (layout.memory_dir / "RULES.md").write_text("# Rules\n", encoding="utf-8")


def test_backup_has_manifest_checksums_and_owner_only_mode(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _seed(layout)

    summary = create_backup(layout=layout)
    inspected = inspect_backup(summary.path)

    assert inspected.file_count == 5
    assert set(inspected.roots) == {"config", "data", "env", "mcp", "memory"}
    assert stat.S_IMODE(summary.path.stat().st_mode) == 0o600
    with zipfile.ZipFile(summary.path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format_version"] == 1
        assert {item["archive_path"] for item in manifest["files"]} == {
            "config/config.yaml",
            "env/.env",
            "mcp/user.json",
            "data/audit.jsonl",
            "memory/RULES.md",
        }


def test_restore_preview_never_writes(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _seed(layout)
    backup = create_backup(layout=layout)
    (layout.data_dir / "audit.jsonl").write_text("new state\n", encoding="utf-8")

    plan = restore_backup(backup.path, layout=layout)

    assert plan.overwrite_count == 5
    assert plan.recovery_backup is None
    assert (layout.data_dir / "audit.jsonl").read_text(encoding="utf-8") == "new state\n"


def test_force_restore_preserves_new_files_and_creates_rollback(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _seed(layout)
    backup = create_backup(layout=layout)
    (layout.data_dir / "audit.jsonl").write_text("new state\n", encoding="utf-8")
    (layout.data_dir / "created-later.txt").write_text("keep me\n", encoding="utf-8")

    result = restore_backup(backup.path, force=True, layout=layout)

    assert (layout.data_dir / "audit.jsonl").read_text(encoding="utf-8") == '{"event": 1}\n'
    assert (layout.data_dir / "created-later.txt").read_text(encoding="utf-8") == "keep me\n"
    assert result.recovery_backup is not None
    rollback = inspect_backup(result.recovery_backup)
    assert rollback.file_count == 6


def test_checksum_tampering_is_rejected(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _seed(layout)
    backup = create_backup(layout=layout)
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(backup.path) as source, zipfile.ZipFile(tampered, "w") as target:
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "data/audit.jsonl":
                payload = b"tampered\n"
            target.writestr(info, payload)

    with pytest.raises(BackupValidationError, match="checksum mismatch|size mismatch"):
        inspect_backup(tampered)


def test_path_traversal_is_rejected_before_planning(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    manifest = {
        "format": "trade-compass-backup",
        "format_version": 1,
        "app_version": "test",
        "created_at": "2026-01-01T00:00:00+00:00",
        "files": [],
    }
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("manifest.json", json.dumps(manifest))
        target.writestr("../escape", "bad")

    with pytest.raises(BackupValidationError, match="Unsafe backup path"):
        plan_restore(archive, layout=_layout(tmp_path))


def test_symlink_is_rejected_instead_of_followed(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _seed(layout)
    outside = tmp_path / "outside-secret"
    outside.write_text("do not archive", encoding="utf-8")
    (layout.data_dir / "secret-link").symlink_to(outside)

    with pytest.raises(RecoveryError, match="symlink"):
        create_backup(layout=layout)


def test_backup_destination_cannot_be_inside_state(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _seed(layout)

    with pytest.raises(RecoveryError, match="inside runtime state"):
        create_backup(layout.data_dir / "backup.zip", layout=layout)


def test_backup_refuses_to_replace_an_existing_archive(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _seed(layout)
    existing = tmp_path / "existing.zip"
    existing.write_bytes(b"keep")

    with pytest.raises(RecoveryError, match="already exists"):
        create_backup(existing, layout=layout)

    assert existing.read_bytes() == b"keep"


def test_fresh_installed_restore_targets_user_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("TRADE_COMPASS_HOME", str(home))
    monkeypatch.delenv("TRADE_COMPASS_CONFIG", raising=False)
    monkeypatch.setattr("trade_compass_agent.recovery.is_source_checkout", lambda: False)
    monkeypatch.setattr(
        "trade_compass_agent.recovery.resolve_config_path",
        lambda: tmp_path / "read-only-package" / "default.yaml",
    )
    monkeypatch.setattr(
        "trade_compass_agent.recovery.load_app_config",
        lambda: SimpleNamespace(data_dir=home / "data", memory_dir=home / "memory"),
    )

    layout = current_recovery_layout()

    assert layout.config_path == home / "config.yaml"
