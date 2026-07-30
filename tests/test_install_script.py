from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tomllib


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fake_uv(path: Path) -> None:
    _write_executable(
        path,
        """#!/bin/sh
set -eu
if [ "$1" = "tool" ] && [ "$2" = "install" ]; then
  printf '%s\\n' "$@" > "$UV_CALL_LOG"
  exit 0
fi
if [ "$1" = "tool" ] && [ "$2" = "dir" ] && [ "$3" = "--bin" ]; then
  printf '%s\\n' "$FAKE_TOOL_BIN"
  exit 0
fi
exit 90
""",
    )


def _fake_trade_compass(path: Path) -> None:
    _write_executable(
        path,
        """#!/bin/sh
set -eu
printf '%s\\n' "$@" >> "$TRADE_CALL_LOG"
if [ "$1" = "--version" ]; then
  printf 'trade-compass-agent 9.9.9\\n'
  exit 0
fi
exit 91
""",
    )


def _base_env(tmp_path: Path, command_dir: Path, tool_bin: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{command_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "FAKE_TOOL_BIN": str(tool_bin),
        "UV_CALL_LOG": str(tmp_path / "uv-call.log"),
        "TRADE_CALL_LOG": str(tmp_path / "trade-call.log"),
        "TRADE_COMPASS_PACKAGE": "trade-compass-agent==9.9.9",
        "TRADE_COMPASS_PYTHON": "3.12",
    }


def test_installer_has_valid_posix_shell_syntax() -> None:
    assert os.access(INSTALLER, os.X_OK)
    result = subprocess.run(
        ["/bin/sh", "-n", str(INSTALLER)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_installer_default_package_matches_project_version() -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]

    assert (
        f'readonly TRADE_COMPASS_DEFAULT_PACKAGE="trade-compass-agent=={version}"'
        in INSTALLER.read_text(encoding="utf-8")
    )


def test_installer_uses_existing_uv_without_running_setup(tmp_path: Path) -> None:
    command_dir = tmp_path / "commands"
    tool_bin = tmp_path / "tool-bin"
    command_dir.mkdir()
    tool_bin.mkdir()
    _fake_uv(command_dir / "uv")
    _fake_trade_compass(tool_bin / "trade-compass")

    result = subprocess.run(
        ["/bin/sh", str(INSTALLER)],
        env=_base_env(tmp_path, command_dir, tool_bin),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "欢迎使用 Trade Compass Agent" in result.stdout
    assert "trade-compass-agent 9.9.9" in result.stdout
    assert "trade-compass setup" in result.stdout
    assert f'export PATH="{tool_bin}:$PATH"' in result.stdout
    uv_args = (tmp_path / "uv-call.log").read_text(encoding="utf-8").splitlines()
    assert uv_args == [
        "tool",
        "install",
        "--upgrade",
        "--python",
        "3.12",
        "trade-compass-agent==9.9.9",
    ]
    trade_args = (tmp_path / "trade-call.log").read_text(encoding="utf-8").splitlines()
    assert trade_args == ["--version"]
    assert not (tmp_path / "home" / ".trade-compass").exists()


def test_installer_bootstraps_pinned_uv_without_modifying_shell(tmp_path: Path) -> None:
    command_dir = tmp_path / "commands"
    tool_bin = tmp_path / "tool-bin"
    command_dir.mkdir()
    tool_bin.mkdir()
    fake_uv_source = tmp_path / "fake-uv"
    _fake_uv(fake_uv_source)
    _fake_trade_compass(tool_bin / "trade-compass")

    remote_installer = tmp_path / "remote-uv-installer.sh"
    _write_executable(
        remote_installer,
        """#!/bin/sh
set -eu
printf '%s\\n' "${UV_NO_MODIFY_PATH:-}" > "$UV_INSTALL_ENV_LOG"
mkdir -p "$UV_INSTALL_DIR"
cp "$FAKE_UV_SOURCE" "$UV_INSTALL_DIR/uv"
chmod 755 "$UV_INSTALL_DIR/uv"
""",
    )
    _write_executable(
        command_dir / "curl",
        """#!/bin/sh
set -eu
output=""
url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output)
      shift
      output="$1"
      ;;
    http*)
      url="$1"
      ;;
  esac
  shift
done
printf '%s\\n' "$url" > "$CURL_CALL_LOG"
cp "$FAKE_REMOTE_INSTALLER" "$output"
""",
    )

    env = _base_env(tmp_path, command_dir, tool_bin)
    env.update(
        {
            "CURL_CALL_LOG": str(tmp_path / "curl-call.log"),
            "FAKE_REMOTE_INSTALLER": str(remote_installer),
            "FAKE_UV_SOURCE": str(fake_uv_source),
            "TRADE_COMPASS_UV_INSTALLER_SHA256": hashlib.sha256(
                remote_installer.read_bytes()
            ).hexdigest(),
            "UV_INSTALL_ENV_LOG": str(tmp_path / "uv-install-env.log"),
        }
    )
    result = subprocess.run(
        ["/bin/sh", str(INSTALLER)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "home" / ".local" / "bin" / "uv").is_file()
    assert (
        tmp_path / "curl-call.log"
    ).read_text(encoding="utf-8").strip() == "https://astral.sh/uv/0.11.32/install.sh"
    assert (tmp_path / "uv-install-env.log").read_text(encoding="utf-8").strip() == "1"
    assert (tmp_path / "trade-call.log").read_text(encoding="utf-8").splitlines() == [
        "--version"
    ]


def test_installer_rejects_unverified_uv_installer(tmp_path: Path) -> None:
    command_dir = tmp_path / "commands"
    tool_bin = tmp_path / "tool-bin"
    command_dir.mkdir()
    tool_bin.mkdir()
    remote_installer = tmp_path / "remote-uv-installer.sh"
    _write_executable(remote_installer, "#!/bin/sh\nexit 99\n")
    _write_executable(
        command_dir / "curl",
        """#!/bin/sh
set -eu
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output" ]; then
    shift
    output="$1"
  fi
  shift
done
cp "$FAKE_REMOTE_INSTALLER" "$output"
""",
    )

    env = _base_env(tmp_path, command_dir, tool_bin)
    env.update(
        {
            "FAKE_REMOTE_INSTALLER": str(remote_installer),
            "TRADE_COMPASS_UV_INSTALLER_SHA256": "0" * 64,
        }
    )
    result = subprocess.run(
        ["/bin/sh", str(INSTALLER)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "SHA-256 校验失败" in result.stderr
    assert not (tmp_path / "uv-call.log").exists()


def test_installer_rejects_unsupported_platform(tmp_path: Path) -> None:
    command_dir = tmp_path / "commands"
    command_dir.mkdir()
    _write_executable(command_dir / "uname", "#!/bin/sh\nprintf 'FreeBSD\\n'\n")

    result = subprocess.run(
        ["/bin/sh", str(INSTALLER)],
        env={
            **os.environ,
            "PATH": f"{command_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path / "home"),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "目前仅支持 macOS 和 Linux" in result.stderr
