"""Local backup inspection and conservative restore support.

Backups contain logical roots rather than trusted destination paths.  Restore
always maps those roots to the *current* installation's configured locations.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from trade_compass_agent import __version__
from trade_compass_agent.config import (
    PROJECT_ROOT,
    active_env_path,
    is_source_checkout,
    load_app_config,
    resolve_config_path,
    user_config_path,
    user_home_path,
)

BACKUP_FORMAT = "trade-compass-backup"
BACKUP_FORMAT_VERSION = 1
MANIFEST_PATH = "manifest.json"
MAX_ARCHIVE_FILES = 100_000
MAX_UNCOMPRESSED_BYTES = 20 * 1024**3
_SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
_LOGICAL_ROOTS = {"config", "env", "mcp", "data", "memory"}


class RecoveryError(RuntimeError):
    """Base class for backup and restore failures."""


class BackupValidationError(RecoveryError):
    """Raised when an archive is unsafe, unsupported, or corrupt."""


@dataclass(frozen=True)
class RecoveryLayout:
    config_path: Path
    env_path: Path
    mcp_path: Path
    data_dir: Path
    memory_dir: Path
    backup_dir: Path
    project_mcp_path: Path | None = None


@dataclass(frozen=True)
class BackupSummary:
    path: Path
    created_at: str
    app_version: str
    file_count: int
    total_bytes: int
    roots: tuple[str, ...]


@dataclass(frozen=True)
class RestorePlan:
    archive: Path
    files: tuple[tuple[str, Path], ...]
    overwrite_count: int
    create_count: int
    total_bytes: int
    recovery_backup: Path | None = None


def current_recovery_layout() -> RecoveryLayout:
    config = load_app_config()
    source_checkout = is_source_checkout()
    config_path = resolve_config_path()
    if not source_checkout and not os.getenv("TRADE_COMPASS_CONFIG", "").strip():
        config_path = user_config_path()
    return RecoveryLayout(
        config_path=config_path,
        env_path=active_env_path(),
        mcp_path=user_home_path() / "mcp.json",
        data_dir=config.data_dir,
        memory_dir=config.memory_dir,
        backup_dir=user_home_path() / "backups",
        project_mcp_path=PROJECT_ROOT / ".trade-compass" / "mcp.json"
        if source_checkout
        else None,
    )


def create_backup(
    output: Path | None = None,
    *,
    layout: RecoveryLayout | None = None,
) -> BackupSummary:
    """Create an owner-readable ZIP backup with a checksummed manifest."""
    layout = layout or current_recovery_layout()
    destination = _backup_destination(output, layout.backup_dir)
    _reject_destination_inside_state(destination, layout)
    if destination.exists():
        raise RecoveryError(f"Backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    sources = _source_files(layout)
    with tempfile.TemporaryDirectory(prefix="trade-compass-backup-") as temp_name:
        staging = Path(temp_name)
        entries: list[dict[str, Any]] = []
        for archive_path, source in sources:
            staged = staging / archive_path
            staged.parent.mkdir(parents=True, exist_ok=True)
            _snapshot_file(source, staged)
            entries.append(
                {
                    "archive_path": archive_path,
                    "size": staged.stat().st_size,
                    "sha256": _sha256(staged),
                }
            )

        created_at = datetime.now(UTC).isoformat()
        manifest = {
            "format": BACKUP_FORMAT,
            "format_version": BACKUP_FORMAT_VERSION,
            "app_version": __version__,
            "created_at": created_at,
            "files": entries,
            "source": {
                "config_path": str(layout.config_path.resolve()),
                "env_path": str(layout.env_path.resolve()),
                "mcp_path": str(layout.mcp_path.resolve()),
                "data_dir": str(layout.data_dir.resolve()),
                "memory_dir": str(layout.memory_dir.resolve()),
            },
        }
        (staging / MANIFEST_PATH).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _write_archive_atomically(destination, staging, entries)

    os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)
    return _summary(destination, manifest)


def inspect_backup(path: Path) -> BackupSummary:
    """Validate archive structure and every payload checksum."""
    archive = path.expanduser().resolve()
    manifest = _validated_manifest(archive)
    return _summary(archive, manifest)


def plan_restore(
    path: Path,
    *,
    layout: RecoveryLayout | None = None,
) -> RestorePlan:
    """Validate a backup and return its current-installation destination plan."""
    archive = path.expanduser().resolve()
    layout = layout or current_recovery_layout()
    manifest = _validated_manifest(archive)
    planned: list[tuple[str, Path]] = []
    overwrite_count = 0
    for entry in manifest["files"]:
        archive_path = str(entry["archive_path"])
        destination = _restore_destination(archive_path, layout)
        _ensure_destination_within_layout(destination, archive_path, layout)
        planned.append((archive_path, destination))
        overwrite_count += int(destination.exists())
    resolved_destinations = [str(destination.resolve()) for _, destination in planned]
    if len(resolved_destinations) != len(set(resolved_destinations)):
        raise BackupValidationError("Backup maps multiple payloads to one destination")
    return RestorePlan(
        archive=archive,
        files=tuple(planned),
        overwrite_count=overwrite_count,
        create_count=len(planned) - overwrite_count,
        total_bytes=sum(int(entry["size"]) for entry in manifest["files"]),
    )


def restore_backup(
    path: Path,
    *,
    force: bool = False,
    layout: RecoveryLayout | None = None,
) -> RestorePlan:
    """Preview by default; with force, back up current state then restore files.

    Restore is intentionally merge-only: files absent from the backup are never
    deleted.  This prevents an older archive from silently erasing newer state.
    """
    layout = layout or current_recovery_layout()
    plan = plan_restore(path, layout=layout)
    if not force:
        return plan

    recovery = create_backup(layout=layout)
    manifest = _validated_manifest(plan.archive)
    expected = {str(entry["archive_path"]): entry for entry in manifest["files"]}
    with zipfile.ZipFile(plan.archive) as archive:
        with tempfile.TemporaryDirectory(prefix="trade-compass-restore-") as temp_name:
            staging = Path(temp_name)
            for archive_path, destination in plan.files:
                staged = staging / archive_path
                staged.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(archive_path) as source, staged.open("wb") as target:
                    shutil.copyfileobj(source, target)
                entry = expected[archive_path]
                if staged.stat().st_size != int(entry["size"]) or _sha256(staged) != entry["sha256"]:
                    raise BackupValidationError(f"Backup changed during restore: {archive_path}")
                _atomic_replace_from(staged, destination)
                if archive_path.startswith(("config/", "env/", "mcp/")):
                    os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)

    return RestorePlan(
        archive=plan.archive,
        files=plan.files,
        overwrite_count=plan.overwrite_count,
        create_count=plan.create_count,
        total_bytes=plan.total_bytes,
        recovery_backup=recovery.path,
    )


def _source_files(layout: RecoveryLayout) -> list[tuple[str, Path]]:
    result: list[tuple[str, Path]] = []
    for archive_path, path in (
        ("config/config.yaml", layout.config_path),
        ("env/.env", layout.env_path),
        ("mcp/user.json", layout.mcp_path),
    ):
        if path.is_symlink():
            raise RecoveryError(f"Refusing to back up symlink: {path}")
        if path.is_file():
            result.append((archive_path, path))

    if layout.project_mcp_path is not None and layout.project_mcp_path != layout.mcp_path:
        if layout.project_mcp_path.is_symlink():
            raise RecoveryError(f"Refusing to back up symlink: {layout.project_mcp_path}")
        if layout.project_mcp_path.is_file():
            result.append(("mcp/project.json", layout.project_mcp_path))

    for root_name, root in (("data", layout.data_dir), ("memory", layout.memory_dir)):
        if root.is_symlink():
            raise RecoveryError(f"Refusing to back up symlink root: {root}")
        if not root.exists():
            continue
        for source in sorted(root.rglob("*")):
            if source.is_symlink():
                raise RecoveryError(f"Refusing to back up symlink: {source}")
            if not source.is_file() or _is_sqlite_sidecar(source):
                continue
            relative = source.relative_to(root).as_posix()
            result.append((f"{root_name}/{relative}", source))

    archive_paths = [item[0] for item in result]
    if len(archive_paths) != len(set(archive_paths)):
        raise RecoveryError("Backup layout contains duplicate logical paths")
    return sorted(result)


def _snapshot_file(source: Path, destination: Path) -> None:
    if source.suffix.lower() in _SQLITE_SUFFIXES:
        try:
            with sqlite3.connect(source, timeout=30) as source_db:
                with sqlite3.connect(destination) as destination_db:
                    source_db.backup(destination_db)
            return
        except sqlite3.DatabaseError:
            destination.unlink(missing_ok=True)

    for _ in range(3):
        before = source.stat()
        shutil.copy2(source, destination)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns):
            return
    destination.unlink(missing_ok=True)
    raise RecoveryError(f"File changed repeatedly while backing up: {source}")


def _validated_manifest(archive_path: Path) -> dict[str, Any]:
    return _validated_archive_manifest(
        archive_path,
        expected_format=BACKUP_FORMAT,
        expected_version=BACKUP_FORMAT_VERSION,
        allowed_roots=_LOGICAL_ROOTS,
    )


def _validated_archive_manifest(
    archive_path: Path,
    *,
    expected_format: str,
    expected_version: int,
    allowed_roots: set[str],
) -> dict[str, Any]:
    if not archive_path.is_file():
        raise BackupValidationError(f"Backup does not exist: {archive_path}")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_FILES + 1:
                raise BackupValidationError("Backup contains too many files")
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise BackupValidationError("Backup contains duplicate paths")
            for info in infos:
                _validate_archive_path(info.filename)
                if _zipinfo_is_symlink(info):
                    raise BackupValidationError(f"Backup contains a symlink: {info.filename}")
            if MANIFEST_PATH not in names:
                raise BackupValidationError("Backup manifest is missing")
            info_by_name = {info.filename: info for info in infos}
            if info_by_name[MANIFEST_PATH].file_size > 10 * 1024 * 1024:
                raise BackupValidationError("Backup manifest exceeds the safety limit")
            try:
                manifest = json.loads(archive.read(MANIFEST_PATH))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise BackupValidationError("Backup manifest is invalid JSON") from exc
            _validate_manifest_shape(
                manifest,
                expected_format=expected_format,
                expected_version=expected_version,
                allowed_roots=allowed_roots,
            )
            expected = {MANIFEST_PATH, *(str(entry["archive_path"]) for entry in manifest["files"])}
            if set(names) != expected:
                raise BackupValidationError("Backup payload does not match its manifest")
            total = sum(int(entry["size"]) for entry in manifest["files"])
            if total > MAX_UNCOMPRESSED_BYTES:
                raise BackupValidationError("Backup expands beyond the safety limit")
            for entry in manifest["files"]:
                name = str(entry["archive_path"])
                if info_by_name[name].file_size != int(entry["size"]):
                    raise BackupValidationError(f"Backup size mismatch: {name}")
                digest = hashlib.sha256()
                with archive.open(name) as payload:
                    for chunk in iter(lambda: payload.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != entry["sha256"]:
                    raise BackupValidationError(f"Backup checksum mismatch: {name}")
            return manifest
    except zipfile.BadZipFile as exc:
        raise BackupValidationError("Backup is not a valid ZIP archive") from exc


def _validate_manifest_shape(
    manifest: Any,
    *,
    expected_format: str,
    expected_version: int,
    allowed_roots: set[str],
) -> None:
    if not isinstance(manifest, dict):
        raise BackupValidationError("Backup manifest must be an object")
    if manifest.get("format") != expected_format:
        raise BackupValidationError("Unsupported backup format")
    if manifest.get("format_version") != expected_version:
        raise BackupValidationError(
            f"Unsupported backup format version: {manifest.get('format_version')!r}"
        )
    if not isinstance(manifest.get("files"), list):
        raise BackupValidationError("Backup manifest files must be a list")
    if not isinstance(manifest.get("created_at"), str) or not manifest["created_at"]:
        raise BackupValidationError("Backup manifest has no creation time")
    if not isinstance(manifest.get("app_version"), str) or not manifest["app_version"]:
        raise BackupValidationError("Backup manifest has no app version")
    if len(manifest["files"]) > MAX_ARCHIVE_FILES:
        raise BackupValidationError("Backup manifest contains too many files")
    for entry in manifest["files"]:
        if not isinstance(entry, dict):
            raise BackupValidationError("Backup manifest contains an invalid file entry")
        if not {"archive_path", "size", "sha256"} <= entry.keys():
            raise BackupValidationError("Backup manifest file entry is incomplete")
        _validate_archive_path(
            str(entry["archive_path"]),
            allowed_roots=allowed_roots,
        )
        if not isinstance(entry["size"], int) or entry["size"] < 0:
            raise BackupValidationError("Backup manifest contains an invalid file size")
        digest = entry["sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise BackupValidationError("Backup manifest contains an invalid checksum")


def _validate_archive_path(
    value: str,
    *,
    allowed_roots: set[str] | None = None,
) -> None:
    if not value or "\\" in value:
        raise BackupValidationError(f"Unsafe backup path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BackupValidationError(f"Unsafe backup path: {value!r}")
    if allowed_roots is not None and (
        len(path.parts) < 2 or path.parts[0] not in allowed_roots
    ):
        raise BackupValidationError(f"Unknown backup logical root: {value!r}")


def _restore_destination(archive_path: str, layout: RecoveryLayout) -> Path:
    path = PurePosixPath(archive_path)
    root, relative = path.parts[0], path.parts[1:]
    if root == "config":
        if path != PurePosixPath("config/config.yaml"):
            raise BackupValidationError(f"Unknown config payload: {archive_path}")
        return layout.config_path
    if root == "env":
        if path != PurePosixPath("env/.env"):
            raise BackupValidationError(f"Unknown environment payload: {archive_path}")
        return layout.env_path
    if root == "mcp":
        if path.name not in {"mcp.json", "user.json", "project.json"} or len(path.parts) != 2:
            raise BackupValidationError(f"Unknown MCP payload: {archive_path}")
        if path.name == "project.json":
            if layout.project_mcp_path is None:
                raise BackupValidationError(
                    "Project MCP config cannot be restored outside a source checkout"
                )
            return layout.project_mcp_path
        return layout.mcp_path
    base = layout.data_dir if root == "data" else layout.memory_dir
    return base.joinpath(*relative)


def _ensure_destination_within_layout(
    destination: Path,
    archive_path: str,
    layout: RecoveryLayout,
) -> None:
    root = PurePosixPath(archive_path).parts[0]
    if root in {"config", "env", "mcp"}:
        expected = layout.config_path if root == "config" else layout.env_path
        if root == "mcp":
            expected = destination
        if destination.resolve() != expected.resolve():
            raise BackupValidationError(f"Unsafe restore destination: {destination}")
        return
    base = layout.data_dir if root == "data" else layout.memory_dir
    try:
        destination.resolve().relative_to(base.resolve())
    except ValueError as exc:
        raise BackupValidationError(f"Unsafe restore destination: {destination}") from exc


def _atomic_replace_from(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        shutil.copyfile(source, temp_path)
        with temp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)


def _backup_destination(output: Path | None, backup_dir: Path) -> Path:
    if output is not None:
        destination = output.expanduser()
        if not destination.is_absolute():
            destination = (Path.cwd() / destination).resolve()
        return destination
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return backup_dir / f"trade-compass-backup-{timestamp}.zip"


def _reject_destination_inside_state(destination: Path, layout: RecoveryLayout) -> None:
    for root in (layout.data_dir, layout.memory_dir):
        try:
            destination.resolve().relative_to(root.resolve())
        except ValueError:
            continue
        raise RecoveryError(f"Backup destination cannot be inside runtime state: {root}")


def _write_archive_atomically(
    destination: Path,
    staging: Path,
    entries: Iterable[dict[str, Any]],
) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(staging / MANIFEST_PATH, MANIFEST_PATH)
            for entry in entries:
                name = str(entry["archive_path"])
                archive.write(staging / name, name)
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)


def _is_sqlite_sidecar(path: Path) -> bool:
    return any(path.name.endswith(suffix) for suffix in _SQLITE_SIDECARS)


def _zipinfo_is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _summary(path: Path, manifest: dict[str, Any]) -> BackupSummary:
    roots = sorted({str(entry["archive_path"]).split("/", 1)[0] for entry in manifest["files"]})
    return BackupSummary(
        path=path,
        created_at=str(manifest["created_at"]),
        app_version=str(manifest["app_version"]),
        file_count=len(manifest["files"]),
        total_bytes=sum(int(entry["size"]) for entry in manifest["files"]),
        roots=tuple(roots),
    )
