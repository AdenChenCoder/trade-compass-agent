from datetime import datetime

from trade_compass_agent.domain import AccountKind, PaperTrade
from trade_compass_agent.portfolio.simulator import PaperPortfolio


def test_market_price_refresh_has_overall_timeout(monkeypatch):
    import time

    portfolio = PaperPortfolio()
    portfolio.record(PaperTrade(
        symbol="600519",
        account=AccountKind.SHORT_STOCK,
        side="buy",
        quantity=100,
        price=100.0,
        timestamp=datetime.now(),
        reason="test",
    ))
    monkeypatch.setattr(portfolio, "_try_sina_batch_quote", lambda _symbols: None)
    monkeypatch.setattr(
        "trade_compass_agent.portfolio.simulator._MARKET_REFRESH_TIMEOUT_SECONDS",
        0.01,
    )

    class SlowProvider:
        def get_bars(self, *_args, **_kwargs):
            time.sleep(0.1)
            return []

    started = time.monotonic()
    refreshed = portfolio.refresh_market_prices(SlowProvider())

    assert refreshed == {}
    assert time.monotonic() - started < 0.08


def test_sina_batch_quote_has_hard_timeout(monkeypatch):
    import time

    portfolio = PaperPortfolio()
    portfolio.record(PaperTrade(
        symbol="600519",
        account=AccountKind.SHORT_STOCK,
        side="buy",
        quantity=100,
        price=100.0,
        timestamp=datetime.now(),
        reason="test",
    ))
    monkeypatch.setattr(portfolio, "_try_sina_batch_quote", lambda _symbols: time.sleep(0.2))
    monkeypatch.setattr(
        "trade_compass_agent.portfolio.simulator._SINA_BATCH_TIMEOUT_SECONDS",
        0.01,
    )

    class EmptyProvider:
        def get_bars(self, *_args, **_kwargs):
            return []

    started = time.monotonic()
    refreshed = portfolio.refresh_market_prices(EmptyProvider())

    assert refreshed == {}
    assert time.monotonic() - started < 0.1


def test_t_plus_one_blocks_same_day_sell():
    portfolio = PaperPortfolio()
    portfolio.record(
        PaperTrade(
            symbol="600519",
            account=AccountKind.SHORT_STOCK,
            side="buy",
            quantity=100,
            price=10.0,
            timestamp=datetime(2026, 5, 27, 10, 0),
            reason="test",
        )
    )
    ok, message = portfolio.validate_trade(
        PaperTrade(
            symbol="600519",
            account=AccountKind.SHORT_STOCK,
            side="sell",
            quantity=100,
            price=10.5,
            timestamp=datetime(2026, 5, 27, 14, 0),
            reason="test",
        )
    )
    assert not ok
    assert "T+1" in message


def test_min_lot_no_longer_blocks():
    """Lot size is no longer enforced at OMS level — delegated to agent skills."""
    portfolio = PaperPortfolio()
    ok, message = portfolio.validate_trade(
        PaperTrade(
            symbol="600519",
            account=AccountKind.SHORT_STOCK,
            side="buy",
            quantity=99,
            price=10.0,
            timestamp=datetime(2026, 5, 27, 10, 0),
            reason="test",
        )
    )
    assert ok


def test_limit_up_blocks_buy():
    portfolio = PaperPortfolio()
    limit_ok, limit_msg = portfolio.validate_trade(
        PaperTrade(
            symbol="600519",
            account=AccountKind.SHORT_STOCK,
            side="buy",
            quantity=100,
            price=11.01,
            previous_close=10.0,
            timestamp=datetime(2026, 5, 27, 10, 0),
            reason="test",
            price_limit_pct=0.10,
        )
    )
    assert not limit_ok
    assert "涨停" in limit_msg


def test_st_buy_no_longer_blocks():
    """ST prohibition removed from OMS — delegated to agent skills."""
    portfolio = PaperPortfolio()
    st_ok, st_msg = portfolio.validate_trade(
        PaperTrade(
            symbol="600519",
            account=AccountKind.SHORT_STOCK,
            side="buy",
            quantity=100,
            price=10.0,
            is_st=True,
            timestamp=datetime(2026, 5, 27, 10, 0),
            reason="test",
        )
    )
    assert st_ok


def test_realized_pnl_and_account_summary():
    portfolio = PaperPortfolio()
    portfolio.record(
        PaperTrade(
            symbol="600519",
            account=AccountKind.SHORT_STOCK,
            side="buy",
            quantity=100,
            price=10.0,
            timestamp=datetime(2026, 5, 26, 10, 0),
            reason="test",
        )
    )
    portfolio.record(
        PaperTrade(
            symbol="600519",
            account=AccountKind.SHORT_STOCK,
            side="sell",
            quantity=100,
            price=11.0,
            timestamp=datetime(2026, 5, 27, 10, 0),
            reason="test",
        )
    )
    realized = portfolio.realized_trades()
    summary = [item for item in portfolio.account_summaries() if item.account == AccountKind.SHORT_STOCK][0]
    assert len(realized) == 1
    assert realized[0].pnl > 0
    assert summary.wins == 1
    assert summary.win_rate == 1.0
