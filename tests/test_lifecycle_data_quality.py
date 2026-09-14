import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from trade_compass_agent.data.fundamentals import FundamentalsSnapshot, ChainFundamentalsProvider, EastmoneyDirectFundamentalsProvider
from trade_compass_agent.runtime.tools.market import tool_get_fundamentals


@pytest.mark.parametrize("symbol", ["510210", "515220", "159915", "161725", "sh000001"])
def test_funds_and_indices_do_not_request_corporate_fundamentals(symbol):
    upstream = MagicMock(name="paid")
    chain = ChainFundamentalsProvider([upstream])
    snapshot = chain.get_snapshot(symbol)
    assert not snapshot.has_real_fundamentals
    assert snapshot.data_status == "not_applicable" and snapshot.pe_ttm is None and snapshot.industry is None
    upstream.get_snapshot.assert_not_called()


def test_reported_etf_invalid_fields_cannot_pass_validation_or_tool_output():
    snap = FundamentalsSnapshot(symbol="510210", pe_ttm=0, pb=-2.65, market_cap=0, industry="-2.65")
    stack = SimpleNamespace(provider=SimpleNamespace(get_bars=lambda *a, **k: []),
                            fundamentals_provider=SimpleNamespace(get_snapshot=lambda *a, **k: snap))
    payload = json.loads(tool_get_fundamentals(stack, symbol="510210"))
    assert payload["has_real_fundamentals"] is False and payload["data_status"] == "not_applicable"
    assert all(payload[k] is None for k in ("pe_ttm", "pb", "market_cap", "industry"))


def test_stock_negative_earnings_valid_but_nonfinite_zero_cap_and_numeric_industry_are_not():
    snap = FundamentalsSnapshot(symbol="600001", pe_ttm=-12, pb=-.5, roe=-8, market_cap=0, industry="-2.65", float_shares=float("inf"))
    assert snap.has_real_fundamentals and snap.pe_ttm == -12 and snap.pb == -.5
    assert snap.market_cap is None and snap.industry is None and snap.float_shares is None
    assert snap.notes


def test_shenzhen_identity_mapped_to_correct_market(monkeypatch):
    reply = MagicMock()
    reply.json.return_value = {"data": {"f162": 1234, "f167": 155, "f116": 9999999, "f127": "银行"}}
    get = MagicMock(return_value=reply)
    monkeypatch.setattr("trade_compass_agent.data.fundamentals.requests.get", get)
    monkeypatch.setattr("trade_compass_agent.data.fundamentals.rate_limit_domain", lambda *a: None)
    snapshot = EastmoneyDirectFundamentalsProvider().get_snapshot("000001")
    assert snapshot.pe_ttm == 12.34 and snapshot.industry == "银行"
    assert get.call_args.kwargs["params"]["secid"] == "0.000001"


def test_paid_fundamental_fallback_keeps_failure_reason():
    first = MagicMock()
    first.name = "tushare"
    first.get_snapshot.side_effect = RuntimeError("timeout")
    second = SimpleNamespace(name="free", get_snapshot=lambda *a, **k: FundamentalsSnapshot(symbol="600001", pe_ttm=10, provider_name="free"))
    snapshot = ChainFundamentalsProvider([first, second]).get_snapshot("600001")
    assert snapshot.provider_name == "free" and any("tushare" in n and "timeout" in n.lower() for n in snapshot.notes)


def test_x_search_deadline_does_not_reuse_fast_quote_deadline(monkeypatch):
    from trade_compass_agent.runtime import loop
    from trade_compass_agent.llm.providers import ToolCall
    seen = {}
    def run(fn, timeout, description):
        seen[description] = timeout
        return fn()
    monkeypatch.setattr(loop, "run_with_timeout", run)
    tools = SimpleNamespace(execute=lambda *a: '{}')
    loop._execute_tool_calls([ToolCall(id="x", name="search_x", arguments='{}')], tools, None)
    assert seen["tool:search_x"] >= 30


@pytest.mark.parametrize("paid", [False, True])
@pytest.mark.parametrize("symbol", ["510210", "600519"])
def test_batch_invalid_fundamentals_are_not_reported_as_real(monkeypatch, paid, symbol):
    from trade_compass_agent.runtime.tools.batch import tool_batch_get_fundamentals
    provider = SimpleNamespace(name="tushare", get_snapshots=lambda codes: {})
    stack = SimpleNamespace(fundamentals_provider=SimpleNamespace(providers=[provider] if paid else []))
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"data": {"diff": [
        {"f12": symbol, "f14": "sample", "f116": 0, "f162": 0, "f167": 0, "f127": 12345}]}})
    monkeypatch.setattr("trade_compass_agent.runtime.tools.batch.requests.get", lambda *a, **k: response)
    monkeypatch.setattr("trade_compass_agent.runtime.tools.batch.rate_limit_domain", lambda *a: None)
    payload = json.loads(tool_batch_get_fundamentals(stack, symbols=symbol))
    row = payload["results"][symbol]
    assert row["has_real_fundamentals"] is False, row
    assert row.get("industry") is None


def test_batch_preserves_valid_stock_data_but_not_corporate_metrics_for_funds(monkeypatch):
    from trade_compass_agent.runtime.tools import batch
    rows = [{"symbol": symbol, "pe_ttm": -12, "pb": -0.5, "total_market_cap": 1000,
             "float_market_cap": 800, "industry": "银行", "as_of": "2026-09-11",
             "notes": ["source evidence"]} for symbol in ("600519", "510210")]
    monkeypatch.setattr(batch, "_fetch_ulist_batch", lambda _: rows)
    stack = SimpleNamespace(fundamentals_provider=SimpleNamespace(providers=[]))
    payload = json.loads(batch.tool_batch_get_fundamentals(stack, symbols="600519,510210"))
    stock, fund = payload["results"]["600519"], payload["results"]["510210"]
    assert stock["has_real_fundamentals"] and stock["data_status"] == "available"
    assert stock["pe_ttm"] == -12 and stock["pb"] == -0.5 and stock["industry"] == "银行"
    assert stock["as_of"] == "2026-09-11" and stock["notes"] == ["source evidence"]
    assert stock["provider"] == "eastmoney_ulist_batch"
    assert not fund["has_real_fundamentals"] and fund["data_status"] == "not_applicable"
    assert all(fund[field] is None for field in ("pe_ttm", "pb", "total_market_cap", "float_market_cap", "industry"))
