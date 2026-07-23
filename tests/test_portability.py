from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from pathlib import Path

import pytest
import yaml

from trade_compass_agent.portability import (
    PORTABLE_FORMAT,
    PORTABLE_PRIVACY,
    create_portable_export,
    import_portable_export,
    inspect_portable_export,
    plan_import,
)
from trade_compass_agent.recovery import (
    BackupValidationError,
    RecoveryLayout,
    create_backup,
    inspect_backup,
)


def _layout(tmp_path: Path, name: str) -> RecoveryLayout:
    root = tmp_path / name
    return RecoveryLayout(
        config_path=root / "config.yaml",
        env_path=root / ".env",
        mcp_path=root / "mcp.json",
        data_dir=root / "custom-data",
        memory_dir=root / "custom-memory",
        backup_dir=root / "backups",
    )


def _seed(layout: RecoveryLayout, *, value: str = "source") -> None:
    layout.config_path.parent.mkdir(parents=True)
    layout.config_path.write_text(
        yaml.safe_dump(
            {
                "profile": "local",
                "data_dir": str(layout.data_dir),
                "memory_dir": str(layout.memory_dir),
                "llm": {"api_key_env": "DEEPSEEK_API_KEY", "api_key": "literal-secret"},
                "integration": {"password": "also-secret"},
                "context_compression": {"chars_per_token": 1.5},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    layout.env_path.write_text("DEEPSEEK_API_KEY=env-secret\n", encoding="utf-8")
    layout.mcp_path.write_text('{"token": "mcp-secret"}\n', encoding="utf-8")
    layout.data_dir.mkdir()
    layout.memory_dir.mkdir()
    (layout.data_dir / "agent_sessions.jsonl").write_text(
        f'{{"message": "{value} free text may still be sensitive"}}\n',
        encoding="utf-8",
    )
    (layout.data_dir / "weixin_credentials.json").write_text(
        '{"token": "known-secret"}\n', encoding="utf-8"
    )
    (layout.data_dir / "client.key").write_text("private-key\n", encoding="utf-8")
    (layout.memory_dir / "RULES.md").write_text(f"# {value} rules\n", encoding="utf-8")


def test_portable_export_normalizes_config_and_excludes_known_credentials(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path, "source")
    _seed(layout)

    summary = create_portable_export(layout=layout)
    inspected = inspect_portable_export(summary.path)

    assert inspected.excluded_count == 2
    assert set(inspected.redacted_config_keys) == {"integration.password", "llm.api_key"}
    assert stat.S_IMODE(summary.path.stat().st_mode) == 0o600
    with zipfile.ZipFile(summary.path) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        config = yaml.safe_load(archive.read("config/config.yaml"))
    assert manifest["format"] == PORTABLE_FORMAT
    assert manifest["privacy"] == PORTABLE_PRIVACY
    assert "env/.env" not in names
    assert "mcp/user.json" not in names
    assert "data/weixin_credentials.json" not in names
    assert "data/client.key" not in names
    assert "data/agent_sessions.jsonl" in names
    assert config["data_dir"] == "data"
    assert config["memory_dir"] == "memory_vault"
    assert config["llm"]["api_key_env"] == "DEEPSEEK_API_KEY"
    assert config["llm"]["api_key"] == ""
    assert config["integration"]["password"] == ""
    assert config["context_compression"]["chars_per_token"] == 1.5


def test_import_preview_is_read_only_and_force_adapts_target_paths(tmp_path: Path) -> None:
    source = _layout(tmp_path, "source")
    target = _layout(tmp_path, "target")
    _seed(source)
    _seed(target, value="target")
    portable = create_portable_export(layout=source)
    later = target.data_dir / "created-later.txt"
    later.write_text("preserve\n", encoding="utf-8")

    preview = import_portable_export(portable.path, layout=target)

    assert preview.recovery_backup is None
    assert "target free text" in (target.data_dir / "agent_sessions.jsonl").read_text()

    result = import_portable_export(portable.path, force=True, layout=target)

    imported_config = yaml.safe_load(target.config_path.read_text(encoding="utf-8"))
    assert imported_config["data_dir"] == str(target.data_dir.resolve())
    assert imported_config["memory_dir"] == str(target.memory_dir.resolve())
    assert imported_config["llm"]["api_key"] == ""
    assert "source free text" in (target.data_dir / "agent_sessions.jsonl").read_text()
    assert later.read_text(encoding="utf-8") == "preserve\n"
    assert target.env_path.read_text(encoding="utf-8") == "DEEPSEEK_API_KEY=env-secret\n"
    assert result.recovery_backup is not None
    assert inspect_backup(result.recovery_backup).file_count >= 6
    assert stat.S_IMODE(target.config_path.stat().st_mode) == 0o600


def test_backup_and_portable_formats_cannot_be_confused(tmp_path: Path) -> None:
    layout = _layout(tmp_path, "source")
    _seed(layout)
    backup = create_backup(layout=layout)
    portable = create_portable_export(layout=layout)

    with pytest.raises(BackupValidationError, match="Unsupported backup format"):
        plan_import(backup.path, layout=layout)
    with pytest.raises(BackupValidationError, match="Unsupported backup format"):
        inspect_backup(portable.path)


def test_portable_archive_cannot_smuggle_known_credential_file(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe-portable.zip"
    config_payload = b"data_dir: data\nmemory_dir: memory_vault\n"
    secret_payload = b'{"token": "secret"}\n'
    files = [
        {
            "archive_path": "config/config.yaml",
            "size": len(config_payload),
            "sha256": hashlib.sha256(config_payload).hexdigest(),
        },
        {
            "archive_path": "data/weixin_credentials.json",
            "size": len(secret_payload),
            "sha256": hashlib.sha256(secret_payload).hexdigest(),
        },
    ]
    manifest = {
        "format": PORTABLE_FORMAT,
        "format_version": 1,
        "privacy": PORTABLE_PRIVACY,
        "app_version": "test",
        "created_at": "2026-01-01T00:00:00+00:00",
        "files": files,
        "excluded": [],
        "redacted_config_keys": [],
    }
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("manifest.json", json.dumps(manifest))
        target.writestr("config/config.yaml", config_payload)
        target.writestr("data/weixin_credentials.json", secret_payload)

    with pytest.raises(BackupValidationError, match="excluded payload"):
        inspect_portable_export(archive)
