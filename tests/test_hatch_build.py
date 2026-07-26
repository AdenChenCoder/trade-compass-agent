from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_hatch_build(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    class BuildHookInterface:
        pass

    module_names = (
        "hatchling",
        "hatchling.builders",
        "hatchling.builders.hooks",
        "hatchling.builders.hooks.plugin",
        "hatchling.builders.hooks.plugin.interface",
    )
    modules = {name: ModuleType(name) for name in module_names}
    modules["hatchling.builders.hooks.plugin.interface"].BuildHookInterface = BuildHookInterface
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location(
        "_test_hatch_build_module",
        PROJECT_ROOT / "hatch_build.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_editable_build_skips_frontend_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_hatch_build(monkeypatch)
    dependencies_available = Mock(side_effect=AssertionError("must not inspect frontend"))
    build_frontend = Mock(side_effect=AssertionError("must not build frontend"))
    monkeypatch.setattr(module, "_frontend_dependencies_available", dependencies_available)
    monkeypatch.setattr(module, "_build_frontend", build_frontend)

    build_data = {"artifacts": []}
    module.CustomBuildHook().initialize("editable", build_data)

    dependencies_available.assert_not_called()
    build_frontend.assert_not_called()
    assert build_data["artifacts"] == []


def test_standard_build_still_requires_frontend_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_hatch_build(monkeypatch)
    monkeypatch.setattr(module, "WEB_DIST", tmp_path / "web_dist")
    monkeypatch.setattr(module, "_frontend_dependencies_available", lambda: False)

    with pytest.raises(RuntimeError, match="frontend dependencies are unavailable"):
        module.CustomBuildHook().initialize("standard", {"artifacts": []})


def test_standard_build_bundles_frontend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_hatch_build(monkeypatch)
    web_dist = tmp_path / "web_dist"
    monkeypatch.setattr(module, "WEB_DIST", web_dist)
    monkeypatch.setattr(module, "_frontend_dependencies_available", lambda: True)

    def build_frontend() -> None:
        web_dist.mkdir()
        (web_dist / "index.html").write_text("<!doctype html>", encoding="utf-8")

    monkeypatch.setattr(module, "_build_frontend", build_frontend)

    build_data = {"artifacts": []}
    module.CustomBuildHook().initialize("standard", build_data)

    assert build_data["artifacts"] == ["src/trade_compass_agent/web_dist/**"]
