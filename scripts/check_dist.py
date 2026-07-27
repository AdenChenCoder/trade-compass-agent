#!/usr/bin/env python3
"""Validate release archives before they are uploaded."""

from __future__ import annotations

from email.parser import BytesParser
from email.policy import default
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
}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".tsbuildinfo"}
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
    "trade_compass_agent/web_dist/index.html",
    "trade_compass_agent/workflows/catalyst_calendar_cn/workflow.yaml",
}
REQUIRED_BASE_DEPENDENCIES = {
    "akshare",
    "baostock",
    "duckduckgo-search",
    "fastapi",
    "numpy",
    "openai",
    "pandas",
    "pydantic",
    "python-dotenv",
    "pyyaml",
    "uvicorn",
}
FORBIDDEN_BASE_DEPENDENCIES = {"duckdb"}
REQUIRED_PROJECT_URLS = {"Changelog", "Documentation", "Homepage", "Issues", "Repository"}
MAX_WHEEL_SIZE_BYTES = 5 * 1024 * 1024


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

    if wheel.stat().st_size > MAX_WHEEL_SIZE_BYTES:
        print(
            f"{wheel.name}: wheel exceeds {MAX_WHEEL_SIZE_BYTES // (1024 * 1024)} MiB budget",
            file=sys.stderr,
        )
        return 1

    sdist_prefix = f"trade_compass_agent-{version}/"
    required_sdist_files = {
        f"{sdist_prefix}src/trade_compass_agent/web_dist/index.html",
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
