"""Portfolio management tools — analyze, trade, and monitor positions."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from trade_compass_agent.domain import AccountKind, PaperTrade
from trade_compass_agent.evaluation.signal_tracker import SignalTracker
from trade_compass_agent.portfolio import JsonPaperPortfolio
from trade_compass_agent.portfolio.lot_sizing import (
    pnl_exit_review_candidate,
    suggest_rebalance_for_pnl,
)
from trade_compass_agent.portfolio.market_rules import infer_market_rules
from trade_compass_agent.runtime.market_stack import MarketStack

logger = logging.getLogger(__name__)

_EXECUTION_PRICE_SOURCES = {
    "market_quote",
    "broker_fill",
    "user_confirmed",
    "provided_execution",
    "external_import",
}


def _get_portfolio(stack: MarketStack) -> JsonPaperPortfolio:
    return JsonPaperPortfolio(
        stack.config.data_dir / "paper_trades.jsonl",
        costs=stack.config.trading_costs,
    )


def _fetch_exit_review_market_context(symbols: list[str]) -> dict[str, Any]:
    """Optional sector/main-force context for exit_review positions."""
    try:
        from trade_compass_agent.data.fund_flow import FundFlowProvider

        provider = FundFlowProvider()
        industry = provider.get_sector_flow("industry", limit=5)
        main = provider.get_stock_main_flow(limit=80)
        ctx: dict[str, Any] = {}
        if industry:
            ctx["industry_flow_top5"] = [
                {"name": s.sector_name, "pct": s.change_pct, "net_inflow_yi": s.net_inflow}
                for s in industry
            ]
        by_symbol = {s.symbol: s.main_net_inflow for s in main}
        sym_ctx = {sym: {"main_net_inflow_yi": by_symbol[sym]} for sym in symbols if sym in by_symbol}
        if sym_ctx:
            ctx["symbol_main_flow"] = sym_ctx
        return ctx
    except Exception as exc:
        logger.debug("exit_review market context unavailable: %s", exc)
        return {}


def tool_analyze_portfolio(stack: MarketStack) -> str:
    """Return comprehensive portfolio analysis: positions, P&L, and stats."""
    portfolio = _get_portfolio(stack)
    try:
        positions = portfolio.positions_with_market_prices(provider=stack.provider)
        if positions and not isinstance(positions[0].market_value, (int, float)):
            raise TypeError("non-numeric position data")
    except Exception:
        positions = portfolio.positions()
    summaries = portfolio.account_summaries()
    realized = portfolio.realized_trades()

    position_data = []
    review_symbols: list[str] = []
    for p in positions:
        rules = infer_market_rules(p.symbol)
        pnl_pct = round((p.last_price / p.avg_cost - 1) * 100, 2) if p.avg_cost > 0 else 0
        is_min_lot = p.quantity <= rules.min_lot
        rebalance = suggest_rebalance_for_pnl(p.quantity, rules.min_lot, pnl_pct)
        exit_review = pnl_exit_review_candidate(pnl_pct)
        if exit_review:
            review_symbols.append(p.symbol)
        entry = {
            "symbol": p.symbol,
            "account": p.account.value,
            "quantity": p.quantity,
            "min_lot": rules.min_lot,
            "is_min_lot": is_min_lot,
            "avg_cost": p.avg_cost,
            "last_price": p.last_price,
            "price_source": p.price_source,
            "price_is_fresh": p.price_is_fresh,
            "market_value": p.market_value,
            "unrealized_pnl": p.unrealized_pnl,
            "pnl_pct": pnl_pct,
        }
        if not p.price_is_fresh:
            entry["price_warning"] = "未获取到实时/最新行情，last_price 来自最近成交或成本回退；不要据此判断成本价持平"
        if is_min_lot:
            entry["lot_note"] = f"持仓{p.quantity}股=最小手数，无法部分减仓，只能全部卖出或继续持有"
        if rebalance:
            entry["rebalance_hint"] = rebalance
        if exit_review:
            entry["exit_review"] = exit_review
            entry["exit_review_suggested"] = True
        position_data.append(entry)

    if review_symbols:
        market_ctx = _fetch_exit_review_market_context(review_symbols)
        if market_ctx:
            for entry in position_data:
                if not entry.get("exit_review_suggested"):
                    continue
                sym = entry["symbol"]
                sym_ctx: dict[str, Any] = {}
                if "industry_flow_top5" in market_ctx:
                    sym_ctx["industry_flow_top5"] = market_ctx["industry_flow_top5"]
                sym_flow = (market_ctx.get("symbol_main_flow") or {}).get(sym)
                if sym_flow:
                    sym_ctx["symbol_main_flow"] = sym_flow
                if sym_ctx:
                    entry["exit_review_context"] = sym_ctx

    summary_data = [
        {
            "account": s.account.value,
            "position_count": s.position_count,
            "market_value": s.market_value,
            "cost_basis": s.cost_basis,
            "unrealized_pnl": s.unrealized_pnl,
            "realized_pnl": s.realized_pnl,
            "fees": s.fees,
            "win_rate": s.win_rate,
            "payoff_ratio": s.payoff_ratio,
            "max_drawdown": s.max_drawdown,
        }
        for s in summaries
        if s.position_count > 0 or s.realized_pnl != 0
    ]

    recent_closed = [
        {
            "symbol": r.symbol,
            "pnl": r.pnl,
            "entry": r.entry_price,
            "exit": r.exit_price,
            "closed_at": r.closed_at.isoformat(),
        }
        for r in realized[-10:]
    ]

    total_value = sum(p.market_value for p in positions)
    concentration = []
    if total_value > 0:
        for p in sorted(positions, key=lambda x: x.market_value, reverse=True)[:5]:
            concentration.append({
                "symbol": p.symbol,
                "weight_pct": round(p.market_value / total_value * 100, 1),
            })

    return json.dumps(
        {
            "positions": position_data,
            "account_summaries": summary_data,
            "recent_closed_trades": recent_closed,
            "total_market_value": round(total_value, 2),
            "total_positions": len(positions),
            "concentration_top5": concentration,
        },
        ensure_ascii=False,
    )


def tool_place_paper_trade(stack: MarketStack, **kwargs: Any) -> str:
    """Execute a validated paper trade."""
    portfolio = _get_portfolio(stack)

    symbol = str(kwargs.get("symbol") or "")
    side = str(kwargs.get("side") or "")
    quantity = int(kwargs.get("quantity") or 0)
    requested_price = float(kwargs.get("price") or 0)
    reason = str(kwargs.get("reason") or "")
    account = str(kwargs.get("account") or "short_stock")
    trade_id = str(kwargs.get("trade_id") or uuid4().hex[:16])
    record_decision = bool(kwargs.get("record_decision", True))
    decision_id = (
        str(kwargs.get("decision_id") or uuid4().hex[:8])
        if side == "buy" and record_decision
        else None
    )
    price_source = str(kwargs.get("price_source") or "provided_execution")

    if price_source not in _EXECUTION_PRICE_SOURCES:
        return json.dumps({"error": f"无效价格来源: {price_source}"}, ensure_ascii=False)

    if not symbol or not side or quantity <= 0:
        return json.dumps({"error": "缺少必要参数: symbol, side, quantity, price"}, ensure_ascii=False)

    price_as_of = None
    price = requested_price
    if price_source == "market_quote":
        try:
            price, price_as_of = _latest_execution_price(stack, symbol)
        except Exception as exc:
            return json.dumps(
                {"error": f"无法获取 {symbol} 的市场成交价: {exc}", "trade_rejected": True},
                ensure_ascii=False,
            )
    elif price <= 0:
        return json.dumps({"error": "缺少必要参数: price"}, ensure_ascii=False)

    if side not in ("buy", "sell"):
        return json.dumps({"error": "side 必须为 'buy' 或 'sell'"}, ensure_ascii=False)

    try:
        acct = AccountKind(account)
    except ValueError:
        return json.dumps(
            {"error": f"无效账户类型: {account}, 可选: {[a.value for a in AccountKind]}"},
            ensure_ascii=False,
        )

    rules = infer_market_rules(symbol)
    previous_close = kwargs.get("previous_close")
    is_st = bool(kwargs.get("is_st", False))
    suspended = bool(kwargs.get("suspended", False))
    is_t0 = kwargs.get("is_t0")
    if is_t0 is None:
        is_t0 = rules.is_t0
    price_limit_pct = kwargs.get("price_limit_pct")
    if price_limit_pct is None:
        price_limit_pct = rules.price_limit_pct

    trade = PaperTrade(
        symbol=symbol,
        account=acct,
        side=side,
        quantity=quantity,
        price=price,
        timestamp=datetime.now(),
        reason=reason,
        trade_id=trade_id,
        decision_id=decision_id,
        price_source=price_source,
        price_as_of=price_as_of,
        requested_price=requested_price if requested_price > 0 else None,
        previous_close=float(previous_close) if previous_close is not None else None,
        suspended=suspended,
        is_st=is_st,
        is_t0=bool(is_t0),
        price_limit_pct=float(price_limit_pct),
    )

    ok, msg = portfolio.validate_trade(trade)
    if not ok:
        return json.dumps({"error": msg, "trade_rejected": True}, ensure_ascii=False)

    fee = portfolio.estimate_fee(trade)
    portfolio.record(trade)

    _update_signal_tracker(stack.config.data_dir, symbol, side, price)

    # Decision Journal + Instrument Page integration
    _sync_decision_journal(
        stack.config.data_dir,
        stack.config.memory_dir,
        stack.config.trading_costs,
        trade,
    )

    held = next((p for p in portfolio.positions() if p.symbol == symbol and p.account == account), None)
    if side == "sell":
        position_after = {"quantity": held.quantity if held else 0, "closed": held is None}
    else:
        position_after = {"quantity": held.quantity if held else 0, "avg_cost": round(held.avg_cost, 3) if held else 0}

    return json.dumps(
        {
            "status": "executed",
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "fee": fee,
            "account": account,
            "reason": reason,
            "trade_id": trade_id,
            "decision_id": decision_id,
            "price_source": price_source,
            "price_as_of": price_as_of.isoformat() if price_as_of else None,
            "requested_price": requested_price if requested_price > 0 else None,
            "position_after": position_after,
        },
        ensure_ascii=False,
    )


def tool_check_exit_signals(stack: MarketStack) -> str:
    """Check each held position for potential exit signals."""
    portfolio = _get_portfolio(stack)
    positions = portfolio.positions()
    if not positions:
        return json.dumps({"alerts": [], "message": "无持仓"}, ensure_ascii=False)

    signal_map = _load_signal_map(stack.config.data_dir)

    alerts: list[dict[str, Any]] = []
    for pos in positions:
        symbol = pos.symbol
        reasons: list[str] = []

        signal = signal_map.get(symbol)
        if signal:
            if signal.get("stop_loss") and pos.last_price <= signal["stop_loss"]:
                reasons.append(f"触发止损: 当前价{pos.last_price} <= 止损价{signal['stop_loss']}")
            if signal.get("target_price") and pos.last_price >= signal["target_price"]:
                reasons.append(f"到达目标: 当前价{pos.last_price} >= 目标价{signal['target_price']}")

        if pos.avg_cost > 0:
            drawdown_pct = (pos.last_price / pos.avg_cost - 1) * 100
            if drawdown_pct <= -8.0:
                reasons.append(f"亏损过大: {drawdown_pct:.1f}% 从成本")

        if reasons:
            rules = infer_market_rules(symbol)
            is_min_lot = pos.quantity <= rules.min_lot
            alerts.append({
                "symbol": symbol,
                "quantity": pos.quantity,
                "min_lot": rules.min_lot,
                "is_min_lot": is_min_lot,
                "avg_cost": pos.avg_cost,
                "last_price": pos.last_price,
                "unrealized_pnl": pos.unrealized_pnl,
                "exit_reasons": reasons,
                "suggested_action": "sell_all" if is_min_lot else "reduce_or_sell",
                "note": f"持仓={pos.quantity}股=最小手数，只能全卖或不卖" if is_min_lot else None,
            })

    return json.dumps(
        {
            "alerts": alerts,
            "total_positions": len(positions),
            "positions_with_alerts": len(alerts),
        },
        ensure_ascii=False,
    )


def _load_signal_map(data_dir: Path) -> dict[str, dict[str, Any]]:
    """Load latest signal per symbol from signals.jsonl."""
    path = data_dir / "signals.jsonl"
    if not path.exists():
        return {}
    result: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            sig = json.loads(line)
            result[sig["symbol"]] = sig
        except (json.JSONDecodeError, KeyError):
            continue
    return result


def _update_signal_tracker(data_dir: Path, symbol: str, side: str, price: float) -> None:
    """Update signal tracker when a trade is executed."""
    tracker = SignalTracker(data_dir)
    active = tracker.get_active()
    pending = [r for r in tracker._load_all() if r.status == "pending"]

    if side == "buy":
        for record in reversed(pending):
            if record.symbol == symbol:
                tracker.update_entry(record.signal_id, price)
                break
    elif side == "sell":
        for record in reversed(active):
            if record.symbol == symbol:
                days_held = (datetime.now() - datetime.fromisoformat(record.emitted_at)).days
                tracker.update_exit(record.signal_id, price, days_held=days_held)
                break


def tool_batch_paper_trades(stack: MarketStack, **kwargs: Any) -> str:
    """Execute multiple paper trades in one call for bulk position sync."""
    portfolio = _get_portfolio(stack)
    trades_data = kwargs.get("trades", [])
    if not trades_data:
        return json.dumps({"error": "trades 列表不能为空"}, ensure_ascii=False)

    results: list[dict] = []
    for item in trades_data:
        symbol = str(item.get("symbol") or "")
        side = str(item.get("side") or "buy")
        quantity = int(item.get("quantity") or 0)
        price = float(item.get("price") or 0)
        reason = str(item.get("reason") or "批量录入")
        account = str(item.get("account") or "short_stock")
        trade_id = str(item.get("trade_id") or uuid4().hex[:16])
        record_decision = bool(item.get("record_decision", False))
        decision_id = (
            str(item.get("decision_id") or uuid4().hex[:8])
            if side == "buy" and record_decision
            else None
        )
        price_source = str(item.get("price_source") or "external_import")

        if not symbol or quantity <= 0 or price <= 0:
            results.append({"symbol": symbol, "status": "skipped", "error": "参数不完整"})
            continue

        if price_source not in _EXECUTION_PRICE_SOURCES - {"market_quote"}:
            results.append({"symbol": symbol, "status": "skipped", "error": f"无效价格来源: {price_source}"})
            continue

        try:
            acct = AccountKind(account)
        except ValueError:
            results.append({"symbol": symbol, "status": "skipped", "error": f"无效账户: {account}"})
            continue

        rules = infer_market_rules(symbol)
        is_t0 = item.get("is_t0")
        if is_t0 is None:
            is_t0 = rules.is_t0
        price_limit_pct = item.get("price_limit_pct")
        if price_limit_pct is None:
            price_limit_pct = rules.price_limit_pct
        previous_close = item.get("previous_close")

        trade = PaperTrade(
            symbol=symbol,
            account=acct,
            side=side,
            quantity=quantity,
            price=price,
            timestamp=datetime.now(),
            reason=reason,
            trade_id=trade_id,
            decision_id=decision_id,
            price_source=price_source,
            requested_price=price,
            previous_close=float(previous_close) if previous_close is not None else None,
            suspended=bool(item.get("suspended", False)),
            is_st=bool(item.get("is_st", False)),
            is_t0=bool(is_t0),
            price_limit_pct=float(price_limit_pct),
        )

        ok, msg = portfolio.validate_trade(trade)
        if not ok:
            results.append({"symbol": symbol, "status": "rejected", "error": msg})
            continue

        portfolio.record(trade)
        _sync_decision_journal(
            stack.config.data_dir,
            stack.config.memory_dir,
            stack.config.trading_costs,
            trade,
        )
        results.append({
            "symbol": symbol,
            "status": "executed",
            "side": side,
            "quantity": quantity,
            "price": price,
            "trade_id": trade_id,
            "decision_id": decision_id,
            "price_source": price_source,
        })

    executed = sum(1 for r in results if r["status"] == "executed")
    return json.dumps(
        {"total": len(trades_data), "executed": executed, "results": results},
        ensure_ascii=False,
    )


def _sync_decision_journal(
    data_dir: Path,
    memory_dir: Path,
    trading_costs: Any,
    trade: PaperTrade,
) -> None:
    """Rebuild decision outcomes from the persisted ledger and update the instrument page."""
    try:
        from trade_compass_agent.memory.decision_reconciler import reconcile_decisions

        reconcile_decisions(data_dir, trading_costs)
    except Exception as exc:
        logger.warning("Decision journal reconciliation failed: %s", exc)

    try:
        from trade_compass_agent.memory.instrument_store import InstrumentStore

        inst_store = InstrumentStore(memory_dir)
        inst_store.append_trade(
            trade.symbol,
            trade.side,
            trade.quantity,
            trade.price,
            trade.reason,
        )
    except Exception as exc:
        logger.warning("Instrument page update failed: %s", exc)


def _latest_execution_price(stack: MarketStack, symbol: str) -> tuple[float, datetime]:
    bars = stack.provider.get_bars(symbol, timeframe="1m", limit=1)
    if not bars:
        raise ValueError("行情源未返回数据")
    latest = max(bars, key=lambda bar: bar.timestamp)
    price = float(latest.close)
    if price <= 0:
        raise ValueError("行情价格无效")
    return price, latest.timestamp
