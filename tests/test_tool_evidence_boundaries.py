"""Consumer receipts distinguish fetched evidence, returned windows and units."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from trade_compass_agent.config import AppConfig
from trade_compass_agent.data.metadata import bars_metadata
from trade_compass_agent.domain import AccountKind, Bar, PaperTrade
from trade_compass_agent.portfolio import JsonPaperPortfolio
from trade_compass_agent.runtime.tools.batch import tool_batch_get_bars
from trade_compass_agent.runtime.tools.market import tool_get_bars
from trade_compass_agent.runtime.tools.portfolio import tool_analyze_portfolio


@pytest.mark.parametrize("count", [0, 1, 30, 60])
def test_single_and_batch_receipts_explain_the_actual_detail_window(count):
    bars = [Bar("600519", datetime(2026, 1, 1) + timedelta(days=i), 100, 101, 99, 100, 1000,
                source="synthetic") for i in range(count)]
    stack = SimpleNamespace(provider=SimpleNamespace(name="synthetic", get_bars=lambda *a, **kw: bars))
    single = json.loads(tool_get_bars(stack, symbol="600519", limit=60))
    assert single["count"] == count
    assert single["returned_count"] == len(single["bars"]) == min(count, 30)
    assert single["bars_truncated"] is (count > 30)
    batch = json.loads(tool_batch_get_bars(stack, symbols="600519", limit=60, summary_only=False))
    if not count:
        assert batch["errors"]["600519"] == "no data"
    else:
        row = batch["results"]["600519"]
        assert row["bars_count"] == count
        assert row["returned_count"] == len(row["bars"]) == min(count, 30)
        assert row["bars_truncated"] is (count > 30)
        assert row["_meta"]["is_sample"] is True


@pytest.mark.parametrize("source,provider,expected", [("tencent", "cache", "shares"),
    ("tushare", "tushare", "shares"), ("synthetic", "synthetic", "unknown"),
    (None, "akshare", "unknown")])
def test_volume_units_are_only_declared_for_sources_with_proven_normalization(source, provider, expected):
    bars = [Bar("600519", datetime(2026, 1, 1), 100, 101, 99, 100, 17900, source=source)]
    meta = bars_metadata(bars, provider, "1d")
    assert meta["volume_unit"] == expected
    assert bars[0].volume == 17900  # Describing a unit never rewrites observations.
    assert meta["is_sample"] is (source == "synthetic")


def test_mixed_volume_sources_do_not_inherit_one_adapters_unit():
    bars = [Bar("600519", datetime(2026, 1, i + 1), 100, 101, 99, 100, 1000, source=source)
            for i, source in enumerate(["tencent", "akshare"])]
    assert bars_metadata(bars, "chain", "1d")["volume_unit"] == "unknown"


def test_api_and_tool_both_disclose_synthetic_observations(client, monkeypatch):
    bars = [Bar("600519", datetime(2026, 1, 1), 100, 101, 99, 100, 17900, source="synthetic")]
    stack = SimpleNamespace(provider=SimpleNamespace(name="synthetic", get_bars=lambda *a, **kw: bars))
    monkeypatch.setattr("trade_compass_agent.web.api._stack", lambda: stack)
    response = client.get("/api/bars", params={"symbol": "600519", "limit": 1})
    assert response.status_code == 200
    api_meta = response.json()["metadata"]
    tool_meta = json.loads(tool_get_bars(stack, symbol="600519", limit=1))["_meta"]
    for meta in [api_meta, tool_meta]:
        assert meta["is_sample"] is True
        assert meta["freshness"] == "sample"
        assert meta["volume_unit"] == "unknown"


@pytest.mark.parametrize("pairs", [0, 1, 12])
def test_portfolio_full_ledger_count_is_independent_of_recent_details(tmp_path, pairs):
    config = AppConfig(data_dir=tmp_path / "data", memory_dir=tmp_path / "memory")
    portfolio = JsonPaperPortfolio(config.data_dir / "paper_trades.jsonl", costs=config.trading_costs)
    for i in range(pairs):
        for side, price, day in [("buy", 100, 0), ("sell", 101, 1)]:
            portfolio.record(PaperTrade("511880", AccountKind("etf_rotation"), side, 100, price,
                datetime(2026, 1, 1) + timedelta(days=i * 2 + day), "synthetic fixture"))
    stack = SimpleNamespace(config=config, provider=SimpleNamespace())
    result = json.loads(tool_analyze_portfolio(stack))
    assert result["total_trade_count"] == pairs * 2
    assert len(result["recent_trades"]) == min(pairs * 2, 20)
    assert result["trade_history_truncated"] is (pairs * 2 > 20)
    assert len(JsonPaperPortfolio(portfolio.path).trades) == pairs * 2
