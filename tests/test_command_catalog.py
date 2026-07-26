from __future__ import annotations

import json
import sys

from trade_compass_agent import cli
from trade_compass_agent.command_catalog import COMMAND_SPECS, command_catalog


def test_command_paths_and_aliases_are_unique() -> None:
    paths = [path for spec in COMMAND_SPECS for path in (spec.path, *spec.aliases)]

    assert len(paths) == len(set(paths))


def test_commands_json_is_machine_readable(capsys) -> None:
    cli.run_commands(as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["commands"] == command_catalog()
    assert any(item["command"] == "trade-compass memory reindex" for item in payload["commands"])


def test_grouped_data_command_dispatches_existing_behavior(monkeypatch) -> None:
    calls: list[tuple[list[str], str, str | None]] = []
    monkeypatch.setattr(
        cli,
        "run_data_check",
        lambda symbols, *, timeframe, provider: calls.append((symbols, timeframe, provider)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["trade-compass", "data", "check", "600519", "--provider", "akshare"],
    )

    cli.main()

    assert calls == [(["600519"], "1d", "akshare")]


def test_grouped_memory_command_dispatches_existing_behavior(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "run_memory_reindex", lambda: calls.append("reindex"))
    monkeypatch.setattr(
        sys,
        "argv",
        ["trade-compass", "memory", "reindex"],
    )

    cli.main()

    assert calls == ["reindex"]
