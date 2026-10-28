"""Consumer checks for daily signal availability, fills, and portfolio value."""

import pandas as pd
import pytest

from trade_compass_agent.evaluation.backtester import BacktestConfig, run_backtest


def prices(values, *, indexed=False, missing=()):
    days = pd.bdate_range("2026-10-19", periods=len(values))
    frame = pd.DataFrame({
        "date": days, "open": values, "high": values, "low": values,
        "close": values, "volume": [100000] * len(values),
    }).drop(index=list(missing))
    if indexed:
        frame = frame.set_index("date")
    return days, {"600519": frame}


def config(**kwargs):
    return BacktestConfig(
        commission_rate=0, stamp_duty_rate=0, slippage_pct=0,
        min_commission=0, transfer_fee_rate=0, **kwargs,
    )


def test_one_day_signal_cannot_trade_before_it_is_available():
    days, bars = prices([10.0])
    result = run_backtest(pd.DataFrame({"600519": [1.0]}, index=days), bars, config())
    assert result.trades == []
    assert result.open_positions == []
    assert result.final_value == 100000


@pytest.mark.parametrize("indexed", [False, True])
def test_fills_use_next_open_and_leave_terminal_position_open(indexed):
    days, bars = prices([10.0, 11.0, 12.0], indexed=indexed)
    result = run_backtest(pd.DataFrame({"600519": [1.0] * 3}, index=days), bars, config())
    assert result.trades == []
    position = result.open_positions[0]
    assert position.entry_date == "2026-10-20"
    assert position.entry_price == 11
    assert result.final_value == pytest.approx(result.cash + position.quantity * 12)


def test_missing_bar_is_not_a_fill_at_an_old_price():
    days, bars = prices([10.0, 11.0, 12.0], missing=(1,))
    result = run_backtest(pd.DataFrame({"600519": [1.0] * 3}, index=days), bars, config())
    assert result.open_positions[0].entry_date == "2026-10-21"
    assert result.open_positions[0].entry_price == 12


def test_t_plus_one_keeps_position_when_entry_day_touches_stop():
    days, bars = prices([10.0, 10.0, 8.0])
    bars["600519"].loc[1, "low"] = 8
    result = run_backtest(pd.DataFrame({"600519": [1.0, 1.0, 0.0]}, index=days), bars, config())
    assert result.trades[0].entry_date == "2026-10-20"
    assert result.trades[0].exit_date == "2026-10-21"
    assert result.trades[0].exit_price == 8  # Overnight gap, not the planned stop.


def test_costs_reconcile_with_cash_and_net_pnl():
    days, bars = prices([10.0] * 4)
    cfg = BacktestConfig(stop_loss_pct=0.5, take_profit_pct=0.5)
    result = run_backtest(pd.DataFrame({"600519": [1.0, 0.0, 0.0, 0.0]}, index=days), bars, cfg)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.pnl == pytest.approx(-trade.commission_total, abs=0.01)
    assert result.final_value == pytest.approx(cfg.initial_cash + trade.pnl, abs=0.01)
    assert result.daily_nav[-1] == pytest.approx(result.final_value, abs=0.01)


def test_unsorted_price_rows_cannot_reveal_future_prices():
    days, bars = prices([10.0, 11.0, 12.0], indexed=True)
    bars["600519"] = bars["600519"].iloc[::-1]
    result = run_backtest(pd.DataFrame({"600519": [1.0] * 3}, index=days), bars, config())
    assert result.open_positions[0].entry_price == 11


def test_zero_volume_does_not_create_an_execution():
    days, bars = prices([10.0] * 3)
    bars["600519"]["volume"] = 0
    result = run_backtest(pd.DataFrame({"600519": [1.0] * 3}, index=days), bars, config())
    assert result.open_positions == []
    assert result.final_value == 100000


def test_opening_gap_to_target_precedes_later_intraday_stop():
    days, bars = prices([10.0, 10.0, 13.0])
    bars["600519"].loc[2, "low"] = 8
    result = run_backtest(pd.DataFrame({"600519": [1.0] * 3}, index=days), bars, config())
    assert result.trades[0].exit_price == 13
    assert result.trades[0].exit_reason == "take_profit"


def test_stale_terminal_mark_is_not_a_sell_and_open_loss_affects_drawdown():
    days, bars = prices([10.0, 10.0, 9.5, 9.0], missing=(3,))
    result = run_backtest(pd.DataFrame({"600519": [1.0] * 4}, index=days), bars, config(stop_loss_pct=0.5))
    assert not result.trades
    assert result.max_drawdown_pct < 0
    assert result.open_positions[0].price_is_stale
    assert result.open_positions[0].mark_date == "2026-10-21"


def test_undated_prices_are_rejected_instead_of_using_the_last_row():
    days, bars = prices([10.0, 11.0, 12.0])
    bars["600519"] = bars["600519"].drop(columns="date")
    with pytest.raises(ValueError, match="date"):
        run_backtest(pd.DataFrame({"600519": [1.0] * 3}, index=days), bars, config())
