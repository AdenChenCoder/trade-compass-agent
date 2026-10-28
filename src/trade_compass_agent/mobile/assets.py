"""Prepare source-only assets before serving; installed packages stay read-only."""
from pathlib import Path
import runpy
import shutil
import subprocess

from trade_compass_agent.config import PROJECT_ROOT, is_source_checkout

MOBILE_DIST = Path(__file__).resolve().parents[1] / "mobile_dist"
REQUIRED = ("index.html", "sw.js", "manifest.webmanifest", "icon.svg", "icon-192.png", "icon-512.png")


def require_mobile_assets():
    if not all((MOBILE_DIST / name).is_file() for name in REQUIRED) or not any(
        (MOBILE_DIST / "assets").glob("*.js")
    ):
        raise RuntimeError("手机页面尚未准备好，请完成安装并重启交易罗盘后重试")


def prepare_source_mobile():
    """The documented serve command prepares missing/stale source build outputs."""
    if not is_source_checkout():
        return
    source = PROJECT_ROOT / "apps" / "mobile"
    inputs = [source / name for name in ("index.html", "vite.config.ts", "package.json", "tsconfig.json")]
    for directory in (source / "src", source / "public", PROJECT_ROOT / "apps" / "shared"):
        inputs.extend(path for path in directory.rglob("*") if path.is_file())
    try:
        require_mobile_assets()
        current = (MOBILE_DIST / "index.html").stat().st_mtime >= max(path.stat().st_mtime for path in inputs)
    except (OSError, RuntimeError):
        current = False
    if not current:
        pnpm = shutil.which("pnpm")
        if not pnpm or not (source / "node_modules" / ".bin" / "vite").is_file():
            raise RuntimeError("Mobile source prerequisites are missing. Install pnpm and run `pnpm install --frozen-lockfile`, then restart.")
        print("Preparing mobile UI from source...", flush=True)
        # An experimental peer build must never replace the default PWA.
        import os
        env = dict(os.environ)
        env.pop("VITE_COMPASS_TRANSPORT", None)
        try:
            subprocess.run([pnpm, "--dir", "apps/mobile", "build"], cwd=PROJECT_ROOT,
                           env=env, check=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("Mobile UI build failed. Run `pnpm --dir apps/mobile build` to inspect the error, then restart.") from exc
        require_mobile_assets()
    builder = runpy.run_path(str(PROJECT_ROOT / "scripts" / "build_mobile_helper.py"))
    if not builder["bundle_current"]():
        print("Preparing mobile connection component from source...", flush=True)
        try:
            builder["build"]()
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise RuntimeError("Mobile component build failed. Install Go or set TRADE_COMPASS_GO, then run `python scripts/build_mobile_helper.py` and restart.") from exc
