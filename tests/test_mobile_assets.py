import pytest

from trade_compass_agent.mobile import assets


def write_bundle(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for name in assets.REQUIRED:
        (directory / name).write_text("test asset")
    (directory / "assets").mkdir(exist_ok=True)
    (directory / "assets" / "app.js").write_text("test script")


@pytest.fixture
def source(tmp_path, monkeypatch):
    mobile = tmp_path / "apps/mobile"
    mobile.mkdir(parents=True)
    for name in ("index.html", "vite.config.ts", "package.json", "tsconfig.json"):
        (mobile / name).write_text("source input")
    binary = mobile / "node_modules/.bin/vite"
    binary.parent.mkdir(parents=True)
    binary.touch()
    monkeypatch.setattr(assets, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(assets, "MOBILE_DIST", tmp_path / "package/mobile_dist")
    monkeypatch.setattr(assets, "is_source_checkout", lambda: True)
    monkeypatch.setattr(assets.shutil, "which", lambda _: "/pnpm")
    return tmp_path


def test_source_first_start_builds_assets_and_reuses_them_on_restart(source, monkeypatch):
    calls = []
    helper_ready = False

    def build_ui(command, **kwargs):
        calls.append("ui")
        assert command == ["/pnpm", "--dir", "apps/mobile", "build"]
        assert "VITE_COMPASS_TRANSPORT" not in kwargs["env"]
        write_bundle(assets.MOBILE_DIST)

    def build_helper():
        nonlocal helper_ready
        calls.append("helper")
        helper_ready = True

    monkeypatch.setenv("VITE_COMPASS_TRANSPORT", "peer")
    monkeypatch.setattr(assets.subprocess, "run", build_ui)
    monkeypatch.setattr(assets.runpy, "run_path", lambda _: {"build": build_helper, "bundle_current": lambda: helper_ready})
    assets.prepare_source_mobile()
    assets.require_mobile_assets()
    assets.prepare_source_mobile()
    assert calls == ["ui", "helper"]
    assert not (source / "data").exists()
    (assets.MOBILE_DIST / "sw.js").unlink()
    assets.prepare_source_mobile()
    assert calls == ["ui", "helper", "ui"]


def test_missing_source_prerequisite_has_actionable_terminal_error(source, monkeypatch):
    monkeypatch.setattr(assets.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="pnpm install --frozen-lockfile"):
        assets.prepare_source_mobile()
    assert not assets.MOBILE_DIST.exists()


def test_installed_wheel_never_builds_or_writes_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "is_source_checkout", lambda: False)
    monkeypatch.setattr(assets, "MOBILE_DIST", tmp_path / "readonly")
    monkeypatch.setattr(assets.runpy, "run_path", lambda _: pytest.fail("installed package ran a source build"))
    monkeypatch.setattr(assets.subprocess, "run", lambda *_a, **_kw: pytest.fail("installed package launched a build"))
    assets.prepare_source_mobile()
    assert not assets.MOBILE_DIST.exists()
    with pytest.raises(RuntimeError, match="手机页面尚未准备好"):
        assets.require_mobile_assets()


def test_serve_prepares_before_starting_api_and_preserves_workbench_on_failure(monkeypatch, capsys):
    from trade_compass_agent.cli import run_serve
    calls = []

    def prepare():
        calls.append("prepare")
        raise RuntimeError("Install Go or set TRADE_COMPASS_GO")

    monkeypatch.setattr(assets, "prepare_source_mobile", prepare)
    monkeypatch.setattr("trade_compass_agent.daemon.log_rotation.start_launchd_log_rotation", lambda: None)
    monkeypatch.setattr("trade_compass_agent.cli.ensure_runtime_dirs", lambda _: None)
    monkeypatch.setattr("uvicorn.run", lambda *_a, **_kw: calls.append("serve"))
    run_serve("127.0.0.1", 19704, dev=True, open_browser=False, no_scheduler=True)
    assert calls == ["prepare", "serve"]
    assert "TRADE_COMPASS_GO" in capsys.readouterr().err
