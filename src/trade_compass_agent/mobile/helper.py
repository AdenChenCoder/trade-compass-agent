"""Verify packaged helpers, extracting only to the computer's writable state."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import tempfile

BUNDLE = Path(__file__).resolve().parents[1] / "mobile_bin"
PLATFORMS = {("Darwin", "arm64"): "darwin-arm64", ("Darwin", "x86_64"): "darwin-amd64",
             ("Linux", "aarch64"): "linux-arm64", ("Linux", "x86_64"): "linux-amd64"}


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("手机连接状态目录不可用，请检查本机数据目录")
    path.chmod(0o700)


def helper_binary(directory: Path) -> Path:
    target = PLATFORMS.get((platform.system(), platform.machine()))
    if not target:
        raise RuntimeError("当前电脑平台尚不支持跨网手机连接；目前支持 macOS 和 Linux 的 ARM64 / x64")
    try:
        manifest = json.loads((BUNDLE / "manifest.json").read_text())
        entry = manifest["platforms"][target]
        digest, size = entry["sha256"], entry["size"]
        if (manifest["protocol"] != 1 or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or not isinstance(size, int) or not 0 < size <= 128 * 1024 * 1024):
            raise ValueError("invalid helper manifest")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("当前安装缺少有效的手机连接组件，请更新或重新安装交易罗盘") from exc
    private_directory(directory)
    private_directory(directory / "bin")
    destination = directory / "bin" / digest
    private_directory(destination)
    executable = destination / "compass-connect"
    if executable.exists() or executable.is_symlink():
        if not stat.S_ISREG(executable.lstat().st_mode):
            raise RuntimeError("手机连接组件缓存不可用，请检查本机数据目录")
        if executable.stat().st_size == size and hashlib.sha256(executable.read_bytes()).hexdigest() == digest:
            executable.chmod(0o700)
            return executable
    try:
        archive = BUNDLE / (target + ".gz")
        if hashlib.sha256(archive.read_bytes()).hexdigest() != entry["archive_sha256"]:
            raise ValueError("archive checksum mismatch")
        with tempfile.NamedTemporaryFile(dir=destination, delete=False) as output:
            temporary = Path(output.name)
            try:
                checksum, written = hashlib.sha256(), 0
                with gzip.open(archive, "rb") as source:
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        if written > size:
                            raise ValueError("oversized component")
                        checksum.update(chunk)
                        output.write(chunk)
                if written != size or checksum.hexdigest() != digest:
                    raise ValueError("component checksum mismatch")
                output.flush()
                os.fsync(output.fileno())
                temporary.chmod(0o700)
                temporary.replace(executable)
            finally:
                temporary.unlink(missing_ok=True)
    except (OSError, ValueError, KeyError, EOFError) as exc:
        raise RuntimeError("手机连接组件校验未通过，请重新安装交易罗盘后重试") from exc
    return executable


def verify_bundled_helper():
    """Installed-consumer check: execute the selected component without networking."""
    with tempfile.TemporaryDirectory(prefix="compass-helper-check-") as directory:
        executable = helper_binary(Path(directory))
        result = subprocess.run([str(executable), "--version"], capture_output=True,
                                text=True, check=True, timeout=10)
        value = json.loads(result.stdout)
        manifest = json.loads((BUNDLE / "manifest.json").read_text())
        version = manifest.get("tailscale_version", "")
        if (not re.fullmatch(r"\d+\.\d+\.\d+-compass\.[a-f0-9]{12}", version)
                or not version.endswith(manifest["source_sha256"][:12])
                or value != {"name": "compass-connect", "protocol": 1, "tailscale_version": version}):
            raise RuntimeError("Installed mobile component version or protocol mismatch")
        return value
