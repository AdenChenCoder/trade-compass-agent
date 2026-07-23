from trade_compass_agent.portfolio.lot_sizing import (
    format_pnl_alert,
    map_intent_to_sell,
    pnl_exit_review_candidate,
    suggest_rebalance_for_pnl,
    valid_sell_quantities,
)


def test_valid_sell_quantities_100_shares():
    assert valid_sell_quantities(100, 100) == [100]


def test_valid_sell_quantities_300_shares():
    assert valid_sell_quantities(300, 100) == [100, 200, 300]


def test_min_lot_cannot_reduce_third():
    result = map_intent_to_sell(100, 100, "reduce_third")
    assert result["action"] == "sell_all_or_hold"
    assert result["executable"] is False
    assert result["sell_qty"] == 0
    assert "无法部分减仓" in result["note"]


def test_300_shares_reduce_third():
    result = map_intent_to_sell(300, 100, "reduce_third")
    assert result["executable"] is True
    assert result["sell_qty"] == 100
    assert result["remaining_qty"] == 200


def test_200_shares_reduce_third_fallback():
    result = map_intent_to_sell(200, 100, "reduce_third")
    assert result["executable"] is True
    assert result["sell_qty"] == 100
    assert "无法精确" in result["note"]


def test_suggest_rebalance_no_take_profit_on_gain():
    assert suggest_rebalance_for_pnl(100, 100, 24.0) is None


def test_suggest_rebalance_stop_loss_still_works():
    hint = suggest_rebalance_for_pnl(300, 100, -12.0)
    assert hint is not None
    assert hint["trigger"] == "stop_loss_half"
    assert hint["action"] == "partial_sell"


def test_pnl_exit_review_candidate():
    review = pnl_exit_review_candidate(24.0)
    assert review is not None
    assert review["review_only"] is True
    assert review["trigger"] == "pnl_review_mid"
    assert "contextual-take-profit" in review["note"]


def test_format_pnl_alert_min_lot_profit_review():
    msg = format_pnl_alert("600498", 100, 28.0)
    assert msg is not None
    assert "600498" in msg
    assert "8维评分" in msg
    assert "全卖止盈" not in msg


def test_format_pnl_alert_partial_position_review():
    msg = format_pnl_alert("002491", 300, 24.0)
    assert msg is not None
    assert "002491" in msg
    assert "8维评分" in msg or "contextual-take-profit" in msg
