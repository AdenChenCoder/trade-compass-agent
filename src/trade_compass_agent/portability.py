"""Private, reduced-secret archives for moving state between installations."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from yaml.events import AliasEvent

from trade_compass_agent import __version__
from trade_compass_agent.config import invalidate_config_cache
from trade_compass_agent.recovery import (
    MANIFEST_PATH,
    BackupValidationError,
    RecoveryError,
    RecoveryLayout,
    RestorePlan,
    _atomic_replace_from,
    _ensure_destination_within_layout,
    _is_sqlite_sidecar,
    _reject_destination_inside_state,
    _restore_destination,
    _sha256,
    _snapshot_file,
    _validated_archive_manifest,
    _write_archive_atomically,
    create_backup,
    current_recovery_layout,
)

PORTABLE_FORMAT = "trade-compass-portable"
PORTABLE_FORMAT_VERSION = 1
PORTABLE_PRIVACY = "private-migration-not-shareable"
_PORTABLE_ROOTS = {"config", "data", "memory"}
_SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
_SENSITIVE_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "mcp.json",
    "weixin_credentials.json",
}
_SENSITIVE_KEY_NAMES = {
    "access_token",
    "api_key",
    "auth_token",
    "cookie",
    "credential",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
    "webhook_url",
}
_NON_SENSITIVE_CONFIG_KEYS = {"chars_per_token"}


class _NoAliasSafeLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(AliasEvent):
            raise yaml.YAMLError("YAML aliases are not allowed in portable configuration")
        return super().compose_node(parent, index)


@dataclass(frozen=True)
class PortableSummary:
    path: Path
    created_at: str
    app_version: str
    file_count: int
    total_bytes: int
    excluded_count: int
    redacted_config_keys: tuple[str, ...]


def create_portable_export(
    output: Path | None = None,
    *,
    layout: RecoveryLayout | None = None,
) -> PortableSummary:
    """Create a path-neutral archive without known credential files."""
    layout = layout or current_recovery_layout()
    if not layout.config_path.is_file():
        raise RecoveryError("Portable export requires an initialized config; run setup first")
    destination = _export_destination(output, layout)
    _reject_destination_inside_state(destination, layout)
    if destination.exists():
        raise RecoveryError(f"Export destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="trade-compass-export-") as temp_name:
        staging = Path(temp_name)
        config_target = staging / "config/config.yaml"
        config_target.parent.mkdir(parents=True)
        redacted_keys = _write_portable_config(layout.config_path, config_target)

        sources, excluded = _portable_source_files(layout)
        entries = [_manifest_entry("config/config.yaml", config_target)]
        for archive_path, source in sources:
            staged = staging / archive_path
            staged.parent.mkdir(parents=True, exist_ok=True)
            _snapshot_file(source, staged)
            entries.append(_manifest_entry(archive_path, staged))

        manifest = {
            "format": PORTABLE_FORMAT,
            "format_version": PORTABLE_FORMAT_VERSION,
            "privacy": PORTABLE_PRIVACY,
            "app_version": __version__,
            "created_at": datetime.now(UTC).isoformat(),
            "files": sorted(entries, key=lambda entry: str(entry["archive_path"])),
            "excluded": sorted(excluded, key=lambda item: item["archive_path"]),
            "redacted_config_keys": sorted(redacted_keys),
        }
        (staging / MANIFEST_PATH).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _write_archive_atomically(destination, staging, manifest["files"])

    os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)
    return _portable_summary(destination, manifest)


def inspect_portable_export(path: Path) -> PortableSummary:
    archive = path.expanduser().resolve()
    manifest = _validated_portable_manifest(archive)
    return _portable_summary(archive, manifest)


def plan_import(
    path: Path,
    *,
    layout: RecoveryLayout | None = None,
) -> RestorePlan:
    """Validate a portable archive and map it to current configured roots."""
    archive = path.expanduser().resolve()
    layout = layout or current_recovery_layout()
    manifest = _validated_portable_manifest(archive)
    planned: list[tuple[str, Path]] = []
    overwrite_count = 0
    for entry in manifest["files"]:
        archive_path = str(entry["archive_path"])
        destination = _restore_destination(archive_path, layout)
        _ensure_destination_within_layout(destination, archive_path, layout)
        planned.append((archive_path, destination))
        overwrite_count += int(destination.exists())
    resolved = [str(destination.resolve()) for _, destination in planned]
    if len(resolved) != len(set(resolved)):
        raise BackupValidationError("Portable archive maps multiple payloads to one destination")
    return RestorePlan(
        archive=archive,
        files=tuple(planned),
        overwrite_count=overwrite_count,
        create_count=len(planned) - overwrite_count,
        total_bytes=sum(int(entry["size"]) for entry in manifest["files"]),
    )


def import_portable_export(
    path: Path,
    *,
    force: bool = False,
    layout: RecoveryLayout | None = None,
) -> RestorePlan:
    """Preview by default; force creates rollback state and merges the import."""
    layout = layout or current_recovery_layout()
    plan = plan_import(path, layout=layout)
    if not force:
        return plan

    recovery = create_backup(layout=layout)
    manifest = _validated_portable_manifest(plan.archive)
    expected = {str(entry["archive_path"]): entry for entry in manifest["files"]}
    try:
        with zipfile.ZipFile(plan.archive) as archive:
            with tempfile.TemporaryDirectory(prefix="trade-compass-import-") as temp_name:
                staging = Path(temp_name)
                for archive_path, destination in plan.files:
                    staged = staging / archive_path
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(archive_path) as source, staged.open("wb") as target:
                        shutil.copyfileobj(source, target)
                    entry = expected[archive_path]
                    if (
                        staged.stat().st_size != int(entry["size"])
                        or _sha256(staged) != entry["sha256"]
                    ):
                        raise BackupValidationError(
                            f"Portable archive changed during import: {archive_path}"
                        )
                    if archive_path == "config/config.yaml":
                        _adapt_import_config(staged, layout)
                    _atomic_replace_from(staged, destination)
                    if archive_path.startswith("config/"):
                        os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)
    except Exception as exc:
        raise RecoveryError(
            f"Import failed; current-state rollback archive: {recovery.path}"
        ) from exc

    invalidate_config_cache()
    return RestorePlan(
        archive=plan.archive,
        files=plan.files,
        overwrite_count=plan.overwrite_count,
        create_count=plan.create_count,
        total_bytes=plan.total_bytes,
        recovery_backup=recovery.path,
    )


def _portable_source_files(
    layout: RecoveryLayout,
) -> tuple[list[tuple[str, Path]], list[dict[str, str]]]:
    included: list[tuple[str, Path]] = []
    excluded: list[dict[str, str]] = []
    for root_name, root in (("data", layout.data_dir), ("memory", layout.memory_dir)):
        if root.is_symlink():
            raise RecoveryError(f"Refusing to export symlink root: {root}")
        if not root.exists():
            continue
        for source in sorted(root.rglob("*")):
            if source.is_symlink():
                raise RecoveryError(f"Refusing to export symlink: {source}")
            if not source.is_file() or _is_sqlite_sidecar(source):
                continue
            relative = source.relative_to(root).as_posix()
            archive_path = f"{root_name}/{relative}"
            reason = _portable_exclusion_reason(PurePosixPath(archive_path))
            if reason:
                excluded.append({"archive_path": archive_path, "reason": reason})
            else:
                included.append((archive_path, source))
    return included, excluded


def _validated_portable_manifest(archive: Path) -> dict[str, Any]:
    manifest = _validated_archive_manifest(
        archive,
        expected_format=PORTABLE_FORMAT,
        expected_version=PORTABLE_FORMAT_VERSION,
        allowed_roots=_PORTABLE_ROOTS,
    )
    if manifest.get("privacy") != PORTABLE_PRIVACY:
        raise BackupValidationError("Portable archive has no private-migration marker")
    paths = {str(entry["archive_path"]) for entry in manifest["files"]}
    if "config/config.yaml" not in paths:
        raise BackupValidationError("Portable archive has no normalized config")
    for archive_path in paths:
        if reason := _portable_exclusion_reason(PurePosixPath(archive_path)):
            raise BackupValidationError(
                f"Portable archive contains excluded payload ({reason}): {archive_path}"
            )
    with zipfile.ZipFile(archive) as payload:
        config_info = payload.getinfo("config/config.yaml")
        if config_info.file_size > 10 * 1024 * 1024:
            raise BackupValidationError("Portable config exceeds the safety limit")
        try:
            config = _load_yaml(payload.read("config/config.yaml")) or {}
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            raise BackupValidationError("Portable config is invalid YAML") from exc
    if not isinstance(config, dict):
        raise BackupValidationError("Portable config must be a YAML object")
    if config.get("data_dir") != "data" or config.get("memory_dir") != "memory_vault":
        raise BackupValidationError("Portable config paths are not normalized")
    remaining = _sensitive_config_paths(config)
    if remaining:
        raise BackupValidationError(
            f"Portable config contains a sensitive value at: {remaining[0]}"
        )
    return manifest


def _write_portable_config(source: Path, destination: Path) -> list[str]:
    try:
        config = _load_yaml(source.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RecoveryError(f"Cannot export invalid config YAML: {source}") from exc
    if not isinstance(config, dict):
        raise RecoveryError("Portable export requires a YAML object config")
    redacted: list[str] = []
    sanitized = _sanitize_config(config, redacted=redacted)
    sanitized["data_dir"] = "data"
    sanitized["memory_dir"] = "memory_vault"
    destination.write_text(
        yaml.safe_dump(sanitized, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return redacted


def _sanitize_config(
    value: Any,
    *,
    redacted: list[str],
    path: tuple[str, ...] = (),
) -> Any:
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            child_path = (*path, key_text)
            if _is_sensitive_config_key(key_text) and _has_value(item):
                result[key] = ""
                redacted.append(".".join(child_path))
            else:
                result[key] = _sanitize_config(item, redacted=redacted, path=child_path)
        return result
    if isinstance(value, list):
        return [
            _sanitize_config(item, redacted=redacted, path=(*path, str(index)))
            for index, item in enumerate(value)
        ]
    return value


def _sensitive_config_paths(value: Any, path: tuple[str, ...] = ()) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = (*path, str(key))
            if _is_sensitive_config_key(str(key)) and _has_value(item):
                found.append(".".join(child_path))
            else:
                found.extend(_sensitive_config_paths(item, child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_sensitive_config_paths(item, (*path, str(index))))
    return found


def _is_sensitive_config_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    if normalized.endswith("_env") or normalized in _NON_SENSITIVE_CONFIG_KEYS:
        return False
    return normalized in _SENSITIVE_KEY_NAMES or any(
        normalized.endswith(f"_{name}") for name in _SENSITIVE_KEY_NAMES
    )


def _has_value(value: Any) -> bool:
    return value is not None and value != ""


def _portable_exclusion_reason(path: PurePosixPath) -> str | None:
    name = path.name.lower()
    if name in _SENSITIVE_NAMES or name.startswith(".env."):
        return "known credential file"
    if path.suffix.lower() in _SENSITIVE_SUFFIXES:
        return "private key or certificate container"
    if any(marker in name for marker in ("credential", "private_key")):
        return "credential-like filename"
    return None


def _adapt_import_config(path: Path, layout: RecoveryLayout) -> None:
    config = _load_yaml(path.read_text(encoding="utf-8")) or {}
    if not isinstance(config, dict):
        raise BackupValidationError("Portable config must be a YAML object")
    config["data_dir"] = str(layout.data_dir.resolve())
    config["memory_dir"] = str(layout.memory_dir.resolve())
    path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _manifest_entry(archive_path: str, path: Path) -> dict[str, Any]:
    return {
        "archive_path": archive_path,
        "size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _load_yaml(value: str | bytes) -> Any:
    return yaml.load(value, Loader=_NoAliasSafeLoader)


def _export_destination(output: Path | None, layout: RecoveryLayout) -> Path:
    if output is not None:
        destination = output.expanduser()
        return destination if destination.is_absolute() else (Path.cwd() / destination).resolve()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return layout.backup_dir.parent / "exports" / f"trade-compass-export-{timestamp}.zip"


def _portable_summary(path: Path, manifest: dict[str, Any]) -> PortableSummary:
    return PortableSummary(
        path=path,
        created_at=str(manifest["created_at"]),
        app_version=str(manifest["app_version"]),
        file_count=len(manifest["files"]),
        total_bytes=sum(int(entry["size"]) for entry in manifest["files"]),
        excluded_count=len(manifest.get("excluded", [])),
        redacted_config_keys=tuple(str(item) for item in manifest.get("redacted_config_keys", [])),
    )
