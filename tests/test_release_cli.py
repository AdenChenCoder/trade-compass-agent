from __future__ import annotations

from unittest.mock import patch

import pytest

from trade_compass_agent import __version__
from trade_compass_agent.cli import main, run_doctor
from trade_compass_agent.diagnostics import DoctorCheck, _sensitive_file_check


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("sys.argv", ["trade-compass", "--version"]), pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"trade-compass {__version__}"


def test_doctor_exits_nonzero_on_failed_check(
    capsys: pytest.CaptureFixture[str],
) -> None:
    checks = [DoctorCheck("config", "FAIL", "missing")]

    with (
        patch("trade_compass_agent.diagnostics.collect_doctor_checks", return_value=checks),
        pytest.raises(SystemExit) as exc,
    ):
        run_doctor()

    assert exc.value.code == 1
    assert "Doctor: action required" in capsys.readouterr().out


def test_doctor_allows_warnings(capsys: pytest.CaptureFixture[str]) -> None:
    checks = [DoctorCheck("service", "WARN", "foreground mode only")]

    with patch("trade_compass_agent.diagnostics.collect_doctor_checks", return_value=checks):
        run_doctor()

    assert "Doctor: ready" in capsys.readouterr().out


def test_doctor_rejects_insecure_env_permissions(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("DEEPSEEK_API_KEY=secret\n", encoding="utf-8")
    env_path.chmod(0o644)

    check = _sensitive_file_check("env", env_path)

    assert check.status == "FAIL"
    assert "0600" in check.detail


def test_cli_backup_dispatches_output() -> None:
    with (
        patch("sys.argv", ["trade-compass", "backup", "--output", "private.zip"]),
        patch("trade_compass_agent.cli.run_backup") as run_backup,
    ):
        main()

    run_backup.assert_called_once_with(output="private.zip")


def test_cli_backup_inspect_dispatches_archive() -> None:
    with (
        patch("sys.argv", ["trade-compass", "backup", "inspect", "backup.zip"]),
        patch("trade_compass_agent.cli.run_backup_inspect") as inspect,
    ):
        main()

    inspect.assert_called_once_with("backup.zip")


def test_cli_restore_is_preview_unless_forced() -> None:
    with (
        patch("sys.argv", ["trade-compass", "restore", "backup.zip"]),
        patch("trade_compass_agent.cli.run_restore") as restore,
    ):
        main()
    restore.assert_called_once_with("backup.zip", force=False)

    with (
        patch("sys.argv", ["trade-compass", "restore", "backup.zip", "--force"]),
        patch("trade_compass_agent.cli.run_restore") as restore,
    ):
        main()
    restore.assert_called_once_with("backup.zip", force=True)


def test_cli_export_and_inspect_dispatch() -> None:
    with (
        patch("sys.argv", ["trade-compass", "export", "--output", "portable.zip"]),
        patch("trade_compass_agent.cli.run_export") as run_export,
    ):
        main()
    run_export.assert_called_once_with(output="portable.zip")

    with (
        patch("sys.argv", ["trade-compass", "export", "inspect", "portable.zip"]),
        patch("trade_compass_agent.cli.run_export_inspect") as inspect,
    ):
        main()
    inspect.assert_called_once_with("portable.zip")


def test_cli_import_is_preview_unless_forced() -> None:
    with (
        patch("sys.argv", ["trade-compass", "import", "portable.zip"]),
        patch("trade_compass_agent.cli.run_import") as run_import,
    ):
        main()
    run_import.assert_called_once_with("portable.zip", force=False)

    with (
        patch("sys.argv", ["trade-compass", "import", "portable.zip", "--force"]),
        patch("trade_compass_agent.cli.run_import") as run_import,
    ):
        main()
    run_import.assert_called_once_with("portable.zip", force=True)


@pytest.mark.parametrize(
    "argv",
    [
        ["trade-compass", "service", "verify", "--json"],
        ["trade-compass", "service", "--json", "verify"],
    ],
)
def test_cli_service_verify_accepts_json_before_or_after_subcommand(argv: list[str]) -> None:
    with (
        patch("sys.argv", argv),
        patch("trade_compass_agent.cli.load_project_dotenv"),
        patch("trade_compass_agent.cli.setup_logging"),
        patch("trade_compass_agent.cli._resolve_port", return_value=19704),
        patch("trade_compass_agent.daemon.cli.run_service_command") as run_service,
    ):
        main()

    run_service.assert_called_once_with(
        "verify",
        port=19704,
        host="127.0.0.1",
        force=False,
        as_json=True,
    )
