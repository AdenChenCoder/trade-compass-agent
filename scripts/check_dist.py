#!/usr/bin/env python3
"""Validate release archives before they are uploaded."""

from __future__ import annotations

from email.parser import BytesParser
from email.policy import default
import gzip
import hashlib
import json
import re
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
FORBIDDEN_PARTS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".uv-cache",
    ".venv",
    "__pycache__",
    "graphify-out",
    "node_modules",
    ".gradle",
    ".kotlin",
    "test-results",
    "playwright-report",
}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".tsbuildinfo", ".apk", ".jks", ".keystore"}
REQUIRED_WHEEL_FILES = {
    "trade_compass_agent/agent_skills.yaml",
    "trade_compass_agent/builtin_skills/investment-masters/references/buffett.md",
    "trade_compass_agent/builtin_skills/investment-masters/SKILL.md",
    "trade_compass_agent/builtin_skills/intraday-tech/SKILL.md",
    "trade_compass_agent/default.yaml",
    "trade_compass_agent/diagnostics.py",
    "trade_compass_agent/env.example",
    "trade_compass_agent/portability.py",
    "trade_compass_agent/recovery.py",
    "trade_compass_agent/setup_wizard.py",
    "trade_compass_agent/daemon/systemd.py",
    "trade_compass_agent/schemas/readers/reader_claims.schema.json",
    "trade_compass_agent/specialists/equity_research/specialist.yaml",
    "trade_compass_agent/web/security.py",
    "trade_compass_agent/mobile/api.py",
    "trade_compass_agent/mobile/client.py",
    "trade_compass_agent/mobile/identity.py",
    "trade_compass_agent/mobile/pairing.py",
    "trade_compass_agent/mobile/peer.py",
    "trade_compass_agent/mobile/server.py",
    "trade_compass_agent/mobile/turns.py",
    "trade_compass_agent/mobile/push.py",
    "trade_compass_agent/mobile/tls.py",
    "trade_compass_agent/mobile/setup.py",
    "trade_compass_agent/mobile/task_push.py",
    "trade_compass_agent/mobile/managed.py",
    "trade_compass_agent/mobile/reachability.py",
    "trade_compass_agent/mobile/helper.py",
    "trade_compass_agent/mobile/tls_reload.py",
    "trade_compass_agent/mobile_bin/manifest.json",
    "trade_compass_agent/mobile_bin/LICENSES.txt",
    "trade_compass_agent/mobile_dist/index.html",
    "trade_compass_agent/mobile_dist/manifest.webmanifest",
    "trade_compass_agent/mobile_dist/sw.js",
    "trade_compass_agent/web_dist/favicon.ico",
    "trade_compass_agent/web_dist/favicon.svg",
    "trade_compass_agent/web_dist/index.html",
    "trade_compass_agent/workflows/catalyst_calendar_cn/workflow.yaml",
}
REQUIRED_BASE_DEPENDENCIES = {
    "aiortc",
    "httpx",
    "akshare",
    "baostock",
    "ddgs",
    "cryptography",
    "pywebpush",
    "fastapi",
    "matplotlib",
    "mplfinance",
    "numpy",
    "openai",
    "pandas",
    "pydantic",
    "python-dotenv",
    "questionary",
    "pyyaml",
    "uvicorn",
}
FORBIDDEN_BASE_DEPENDENCIES = {"duckdb"}
REQUIRED_PROJECT_URLS = {"Changelog", "Documentation", "Homepage", "Issues", "Repository"}
MAX_WHEEL_SIZE_BYTES = 5 * 1024 * 1024
MAX_HELPER_SIZE_BYTES = 40 * 1024 * 1024


def _project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def _validate_names(archive: Path, names: set[str]) -> None:
    errors: list[str] = []
    for name in sorted(names):
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            errors.append(f"unsafe archive path: {name}")
        if FORBIDDEN_PARTS.intersection(path.parts):
            errors.append(f"forbidden build/runtime directory: {name}")
        if path.name == ".env" or path.name == ".DS_Store":
            errors.append(f"forbidden local file: {name}")
        if path.suffix in FORBIDDEN_SUFFIXES:
            errors.append(f"forbidden generated file: {name}")
        parts = path.parts
        if parts and parts[0].startswith("trade_compass_agent-"):
            parts = parts[1:]  # Source distributions have a versioned root.
        if parts and parts[0] in {"data", "memory_vault", "temp"}:
            errors.append(f"forbidden local state: {name}")
        if len(parts) == 2 and parts[0] == "docs" and re.search(r"-20\d{2}-\d{2}-\d{2}\.", parts[1]):
            errors.append(f"forbidden local diagnostic report: {name}")
    if errors:
        raise ValueError(f"{archive.name}:\n  " + "\n  ".join(errors))


def _requirement_name(requirement: str) -> str:
    name = re.split(r"[\s\[<>=!~;(]", requirement, maxsplit=1)[0]
    return name.lower().replace("_", "-")


def _base_dependency_names(metadata: bytes) -> set[str]:
    message = BytesParser(policy=default).parsebytes(metadata)
    requirements = message.get_all("Requires-Dist", [])
    return {
        _requirement_name(requirement)
        for requirement in requirements
        if "extra ==" not in requirement
    }


def _project_url_names(metadata: bytes) -> set[str]:
    message = BytesParser(policy=default).parsebytes(metadata)
    names: set[str] = set()
    for value in message.get_all("Project-URL", []):
        name, separator, _ = value.partition(",")
        if separator and name.strip():
            names.add(name.strip())
    return names


def main() -> int:
    version = _project_version()
    wheel_matches = sorted(DIST.glob(f"trade_compass_agent-{version}-*.whl"))
    sdist = DIST / f"trade_compass_agent-{version}.tar.gz"
    if len(wheel_matches) != 1 or not sdist.is_file():
        print(f"Expected one wheel and one sdist for {version} under {DIST}", file=sys.stderr)
        return 1

    wheel = wheel_matches[0]
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = set(archive.namelist())
        metadata_names = [
            name for name in wheel_names if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_names) != 1:
            print(f"{wheel.name}: expected exactly one METADATA file", file=sys.stderr)
            return 1
        metadata = archive.read(metadata_names[0])
        base_dependencies = _base_dependency_names(metadata)
        project_urls = _project_url_names(metadata)
        helper_size = sum(i.compress_size for i in archive.infolist() if i.filename.startswith("trade_compass_agent/mobile_bin/"))
        try:
            prefix = "trade_compass_agent/mobile_bin/"
            manifest = json.loads(archive.read(prefix + "manifest.json"))
            assert manifest["protocol"] == 1
            assert re.fullmatch(r"\d+\.\d+\.\d+-compass\.[a-f0-9]{12}", manifest["tailscale_version"])
            assert manifest["tailscale_version"].endswith(manifest["source_sha256"][:12])
            assert set(manifest["platforms"]) == {"darwin-arm64", "darwin-amd64", "linux-arm64", "linux-amd64"}
            assert hashlib.sha256(archive.read(prefix + "LICENSES.txt")).hexdigest() == manifest["licenses_sha256"]
            for target, entry in manifest["platforms"].items():
                packed = archive.read(prefix + target + ".gz")
                assert hashlib.sha256(packed).hexdigest() == entry["archive_sha256"]
                binary = gzip.decompress(packed)
                assert len(binary) == entry["size"]
                assert hashlib.sha256(binary).hexdigest() == entry["sha256"]
        except (KeyError, ValueError, AssertionError, OSError) as exc:
            print(f"{wheel.name}: mobile helper bundle is missing or invalid ({type(exc).__name__})", file=sys.stderr)
            return 1
    with tarfile.open(sdist, "r:gz") as archive:
        sdist_names = set(archive.getnames())

    try:
        _validate_names(wheel, wheel_names)
        _validate_names(sdist, sdist_names)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    missing = sorted(REQUIRED_WHEEL_FILES - wheel_names)
    if missing:
        print(f"{wheel.name}: missing required files: {', '.join(missing)}", file=sys.stderr)
        return 1

    missing_dependencies = sorted(REQUIRED_BASE_DEPENDENCIES - base_dependencies)
    forbidden_dependencies = sorted(FORBIDDEN_BASE_DEPENDENCIES & base_dependencies)
    if missing_dependencies or forbidden_dependencies:
        details = []
        if missing_dependencies:
            details.append(f"missing base dependencies: {', '.join(missing_dependencies)}")
        if forbidden_dependencies:
            details.append(
                f"forbidden base dependencies: {', '.join(forbidden_dependencies)}"
            )
        print(f"{wheel.name}: {'; '.join(details)}", file=sys.stderr)
        return 1

    missing_project_urls = sorted(REQUIRED_PROJECT_URLS - project_urls)
    if missing_project_urls:
        print(
            f"{wheel.name}: missing Project-URL metadata: {', '.join(missing_project_urls)}",
            file=sys.stderr,
        )
        return 1

    if wheel.stat().st_size - helper_size > MAX_WHEEL_SIZE_BYTES or helper_size > MAX_HELPER_SIZE_BYTES:
        print(
            f"{wheel.name}: base package exceeds 5 MiB or mobile helpers exceed 40 MiB",
            file=sys.stderr,
        )
        return 1

    sdist_prefix = f"trade_compass_agent-{version}/"
    required_sdist_files = {
        f"{sdist_prefix}src/trade_compass_agent/web_dist/index.html",
        f"{sdist_prefix}src/trade_compass_agent/mobile_bin/manifest.json",
        f"{sdist_prefix}scripts/build_mobile_helper.py",
        f"{sdist_prefix}scripts/mobile-funnel-probe/go.mod",
    }
    missing_sdist = sorted(required_sdist_files - sdist_names)
    if missing_sdist:
        print(
            f"{sdist.name}: missing required files: {', '.join(missing_sdist)}",
            file=sys.stderr,
        )
        return 1

    license_prefix = f"trade_compass_agent-{version}.dist-info/licenses/"
    required_licenses = {
        "LICENSE",
        "THIRD_PARTY_NOTICES.md",
        "src/trade_compass_agent/data/kronos/LICENSE",
    }
    packaged_licenses = {
        name.removeprefix(license_prefix)
        for name in wheel_names
        if name.startswith(license_prefix)
    }
    if not required_licenses.issubset(packaged_licenses):
        print(f"{wheel.name}: required license files are incomplete", file=sys.stderr)
        return 1

    print(
        f"OK - {wheel.name} ({len(wheel_names)} files) and "
        f"{sdist.name} ({len(sdist_names)} files)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
