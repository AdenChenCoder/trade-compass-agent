"""Hatch build hook: bundle Vite static build into the wheel."""

from __future__ import annotations

import shutil
import subprocess
import runpy
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

ROOT = Path(__file__).resolve().parent
WEB_DIST = ROOT / "src" / "trade_compass_agent" / "web_dist"
MOBILE_DIST = ROOT / "src" / "trade_compass_agent" / "mobile_dist"
WEB_NODE_BIN = ROOT / "apps" / "web" / "node_modules" / ".bin"


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        if version == "editable":
            return

        if _frontend_dependencies_available():
            _build_frontend()
        elif not all((bundle / "index.html").is_file() for bundle in (WEB_DIST, MOBILE_DIST)):
            raise RuntimeError(
                "frontend dependencies are unavailable and no prebuilt web_dist/index.html exists"
            )

        if not all((bundle / "index.html").is_file() for bundle in (WEB_DIST, MOBILE_DIST)):
            raise RuntimeError("frontend build completed without web_dist/index.html")

        build_data["artifacts"].append("src/trade_compass_agent/web_dist/**")
        build_data["artifacts"].append("src/trade_compass_agent/mobile_dist/**")
        _build_mobile_helper()
        build_data["artifacts"].append("src/trade_compass_agent/mobile_bin/**")


def _build_mobile_helper() -> None:
    builder = runpy.run_path(str(ROOT / "scripts/build_mobile_helper.py"))
    builder["build"]()


def _build_frontend() -> None:
    if WEB_DIST.exists():
        shutil.rmtree(WEB_DIST)

    try:
        subprocess.run(
            ["pnpm", "--dir", "apps/web", "build"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(["pnpm", "--dir", "apps/mobile", "build"], cwd=ROOT,
                       check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("pnpm is required to build the release web bundle") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "unknown frontend build error").strip()
        raise RuntimeError(f"frontend build failed:\n{detail}") from exc


def _frontend_dependencies_available() -> bool:
    return all((directory / command).is_file() for command in ("tsc", "vite")
               for directory in (WEB_NODE_BIN, ROOT / "apps/mobile/node_modules/.bin"))
