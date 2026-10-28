import json
from dataclasses import replace

import pandas as pd
import pytest

from trade_compass_agent.config import load_app_config
from trade_compass_agent.data.providers import SampleProvider
from trade_compass_agent.evaluation.research import (
    BreakoutParameters, ResearchRequest, ResearchStore, StrategyDefinition,
    breakout_signals, replay_research_run,
)
from trade_compass_agent.runtime.market_stack import MarketStack
from trade_compass_agent.runtime.tools.registry import ToolRegistry


def definition(**kwargs):
    return StrategyDefinition(strategy_id="box-trend", name="趋势突破假设",
                              hypothesis="放量突破后趋势可能延续", invalidation="跌破慢均线", **kwargs)


def test_versions_are_immutable_and_survive_new_store(tmp_path):
    store = ResearchStore(tmp_path)
    first = store.save_strategy(definition())
    assert store.save_strategy(definition()) == first
    second = store.save_strategy(definition(parameters=BreakoutParameters(volume_ratio=1.5)))
    assert first["version"] != second["version"]
    assert len(ResearchStore(tmp_path).list_strategies()) == 2
    assert ResearchStore(tmp_path).get_strategy("box-trend", first["version"]) == first
    with pytest.raises(ValueError):
        store.get_strategy("../../outside", first["version"])


def test_future_bars_cannot_change_earlier_signals():
    frame = pd.DataFrame({"date": pd.bdate_range("2020-01-01", periods=80),
                          "open": range(10, 90), "high": range(11, 91),
                          "low": range(9, 89), "close": range(10, 90), "volume": 1000})
    expected = breakout_signals(frame.iloc[:50], BreakoutParameters())
    frame.loc[50:, ["open", "high", "low", "close", "volume"]] *= 100
    pd.testing.assert_series_equal(expected, breakout_signals(frame, BreakoutParameters()).iloc[:50])


def test_api_tool_restart_and_offline_replay_agree(client, monkeypatch):
    stack = replace(MarketStack.from_config(), provider=SampleProvider())
    registry = ToolRegistry(stack)
    saved = json.loads(registry.execute("save_strategy_research", definition().model_dump()))
    listed = client.get("/api/research/strategies").json()["strategies"]
    assert listed[0]["version"] == saved["version"]
    request = {"strategy_id": "box-trend", "version": saved["version"], "symbol": "600519", "limit": 60}
    monkeypatch.setattr("trade_compass_agent.web.research_api.MarketStack.from_config", lambda: stack)
    response = client.post("/api/research/runs", json=request)
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["evidence_stage"] == "sample_demo"
    assert run["metadata"]["freshness"] == "sample"
    assert run["dataset_sha256"]
    assert {item["name"] for item in run["knowledge_snapshots"]} >= {"strategy-research", "strategy-validation-review"}
    assert run["result"] == replay_research_run(run)
    persisted = ResearchStore(load_app_config().data_dir).get_run(run["run_id"])
    assert persisted == run
    tool = json.loads(registry.execute("get_strategy_research", {"run_id": run["run_id"]}))
    assert tool == run
    assert client.get(f"/api/research/runs/{run['run_id']}").json() == run
    summary = client.get("/api/research/runs").json()["runs"][0]
    assert "dataset" not in summary
    # No simulation fills or trading permission are created by a backtest.
    assert not (stack.config.data_dir / "paper_trades.jsonl").exists()
    assert not (stack.config.data_dir / "autonomous_trading.json").exists()
    # Corrupt evidence cannot be presented as a valid saved experiment.
    path = stack.config.data_dir / "research" / "runs" / f"{run['run_id']}.json"
    tampered = {**run, "symbol": "000001"}
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="校验失败"):
        ResearchStore(stack.config.data_dir).get_run(run["run_id"])


def test_us_hypothesis_is_not_run_with_a_share_execution_model(client):
    saved = client.post("/api/research/strategies", json=definition(market="US").model_dump()).json()
    response = client.post("/api/research/runs", json={
        "strategy_id": "box-trend", "version": saved["version"], "symbol": "600519",
    })
    assert response.status_code == 422
    assert "仅支持 A 股" in response.json()["detail"]


def test_bad_dates_and_unknown_parameters_are_rejected():
    with pytest.raises(ValueError):
        ResearchRequest(strategy_id="box-trend", version="a" * 16, symbol="600519", end_date="2099-01-01")
    with pytest.raises(ValueError):
        definition(parameters={"future_bars": 3})
