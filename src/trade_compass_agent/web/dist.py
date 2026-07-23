from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_web_dist() -> Path | None:
    override = os.getenv("TRADE_COMPASS_WEB_DIST_OVERRIDE")
    if override:
        path = Path(override).resolve()
        if path.is_dir() and (path / "index.html").is_file():
            return path

    try:
        import trade_compass_agent

        packaged = Path(trade_compass_agent.__file__).resolve().parent / "web_dist"
        if packaged.is_dir() and (packaged / "index.html").is_file():
            return packaged
    except (ImportError, TypeError):
        pass

    repo_out = project_root() / "apps" / "web" / "out"
    if repo_out.is_dir() and (repo_out / "index.html").is_file():
        return repo_out

    return None
