
from trade_compass_agent.data.market_pulse import SampleMarketPulseProvider
from trade_compass_agent.runtime.market_stack import MarketStack


def test_sample_market_pulse_has_sector_and_limit_up_summary():
    pulse = SampleMarketPulseProvider().get_market_pulse()
    assert pulse.sectors
    assert pulse.limit_up.count > 0
    assert pulse.notes


def test_market_stack_exposes_pulse_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_COMPASS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRADE_COMPASS_MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("TRADE_COMPASS_DATA_PROVIDER", "sample")
    stack = MarketStack.from_config()
    pulse = stack.market_pulse_provider.get_market_pulse()
    assert pulse.provider_name
