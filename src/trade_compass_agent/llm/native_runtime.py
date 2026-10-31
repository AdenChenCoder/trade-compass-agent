"""Application-owned Codex state with the existing file credential store."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import tomllib
import uuid

from trade_compass_agent.llm.native_environment import native_agent_environment
from trade_compass_agent.runtime.exceptions import AgentUnavailableError


def host_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()


def _reference(path: Path, source: Path) -> None:
    try:
        path.symlink_to(source)
    except FileExistsError:
        if path.is_symlink() and path.resolve() == source.resolve():
            return
        raise AgentUnavailableError("Codex 隔离目录存在冲突文件，请保留现有数据并检查运行目录。")


def prepare_native_runtime(data_dir: Path, conversation=None, *, check=None) -> Path:
    source = host_codex_home()
    runtime = (data_dir / "native_agents" / "codex-runtime").resolve()
    if source == runtime:
        raise AgentUnavailableError("Codex 用户目录与交易罗盘运行目录相同，请分别配置。")
    try:
        settings = tomllib.loads((source / "config.toml").read_text())
    except FileNotFoundError:
        settings = {}
    except (OSError, ValueError) as exc:
        raise AgentUnavailableError("无法读取 Codex 用户配置，请检查配置格式及读取权限。") from exc
    # Never silently replace a keychain policy with a plaintext credential copy.
    if settings.get("cli_auth_credentials_store", "file") != "file":
        raise AgentUnavailableError(
            "当前 Codex 凭据存储方式尚不支持隔离接入；本版本支持 Codex 默认文件凭据存储。"
        )
    if check:
        check()
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    # References preserve login/logout/refresh at the user's existing store;
    # no credentials are read, copied, or rewritten by the application.
    _reference(runtime / "auth.json", source / "auth.json")
    _reference(runtime / "config.toml", source / "config.toml")
    for profile in source.glob("*.config.toml"):
        _reference(runtime / profile.name, profile)
    if conversation is not None:
        # The caller already holds the per-conversation writer guard. Preparing
        # one runtime must not block another session's cancellation or deadline.
        _import_legacy_thread(source, runtime, conversation, check)
    return runtime


def _import_legacy_thread(source: Path, runtime: Path, conversation, check) -> None:
    # Once this cursor has run privately, the host rollout is an older snapshot.
    # A lost private runtime must rebuild from the application transcript, not
    # silently resume the pre-migration copy under the same thread ID.
    if conversation.record.get("runtime_isolated"):
        return
    thread_id = conversation.record.get("thread_id", "")
    try:
        uuid.UUID(thread_id)
    except (ValueError, TypeError, AttributeError):
        return
    pattern = f"**/rollout-*-{thread_id}.jsonl"
    for directory in ("sessions", "archived_sessions"):
        if next((runtime / directory).glob(pattern), None) is not None:
            return
    for directory in ("sessions", "archived_sessions"):
        for path in (source / directory).glob(pattern):
            if check:
                check()
            with path.open() as stream:
                meta = json.loads(stream.readline()).get("payload", {})
            if meta.get("id") != thread_id or Path(meta.get("cwd", "")).resolve() != conversation.cwd.resolve():
                raise AgentUnavailableError("Codex 历史记录的会话归属不匹配，已停止恢复。")
            target = runtime / "sessions" / path.relative_to(source / directory)
            target.parent.mkdir(parents=True, exist_ok=True)
            before = path.stat()
            fd, pending = tempfile.mkstemp(dir=target.parent, prefix=".import-")
            try:
                with os.fdopen(fd, "wb") as output, path.open("rb") as original:
                    while chunk := original.read(65536):
                        if check:
                            check()
                        output.write(chunk)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise AgentUnavailableError("Codex 历史记录仍在变化，请等待当前任务完成后重试。")
                os.replace(pending, target)
            finally:
                Path(pending).unlink(missing_ok=True)
            return


def native_runtime_environment(runtime: Path) -> dict[str, str]:
    env = native_agent_environment()
    # Scope the documented vendor setting to this child; the host is unchanged.
    env["CODEX_HOME"] = str(runtime)
    for key in ("CODEX_SQLITE_HOME", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "CODEX_THREAD_ID"):
        env.pop(key, None)
    return env
