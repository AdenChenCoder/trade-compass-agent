import json
from datetime import datetime
from types import SimpleNamespace

from trade_compass_agent.data.metadata import bars_metadata
from trade_compass_agent.domain import Bar
from trade_compass_agent.runtime.tools.market import tool_get_bars


def test_old_or_cached_bars_are_never_labeled_realtime():
    bars = [Bar("600519", datetime(2020, 1, 2), 10, 11, 9, 10, 1000, source="sina")]
    meta = bars_metadata(bars, "cache", "1d")
    assert meta["freshness"] == "cached"
    assert meta["data_as_of"] == "2020-01-02T00:00:00+08:00"
    assert meta["origin_fetched_at"] is None
    assert meta["retrieved_at"] != meta["data_as_of"]


def test_api_and_agent_share_sample_data_and_as_of_contract(client, monkeypatch):
    from trade_compass_agent.web.api import _stack

    stack = _stack()
    bars = stack.provider.get_bars("600519", timeframe="1d", limit=20)
    provider = SimpleNamespace(name="sample", get_bars=lambda *args, **kwargs: bars)
    stack = SimpleNamespace(provider=provider)
    monkeypatch.setattr("trade_compass_agent.web.api._stack", lambda: stack)
    response = client.get("/api/bars", params={"symbol": "600519", "limit": 20})
    assert response.status_code == 200
    api_meta = response.json()["metadata"]
    tool_meta = json.loads(tool_get_bars(stack, symbol="600519", limit=20))["_meta"]
    assert api_meta["data_as_of"] == tool_meta["data_as_of"]
    assert api_meta["is_sample"] is tool_meta["is_sample"] is True
    assert api_meta["freshness"] == tool_meta["freshness"] == "sample"
