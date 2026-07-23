"""Rebuild Decision Journal outcomes from the authoritative trade ledger."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from trade_compass_agent.config import TradingCostConfig
from trade_compass_agent.concurrency import get_path_lock
from trade_compass_agent.domain import PaperTrade
from trade_compass_agent.memory.decision_store import DecisionStore, TradeDecision
from trade_compass_agent.portfolio import JsonPaperPortfolio
from trade_compass_agent.portfolio.simulator import RealizedTrade


@dataclass(frozen=True)
class ReconciliationResult:
    changed: int
    created: int
    matched: int
    unmatched: int


def reconcile_decisions(
    data_dir: Path,
    costs: TradingCostConfig | None = None,
) -> ReconciliationResult:
    """Idempotently derive decision outcomes from persisted FIFO trade allocations."""
    portfolio = JsonPaperPortfolio(data_dir / "paper_trades.jsonl", costs=costs)
    store = DecisionStore(data_dir)
    lock = get_path_lock(store._file)

    with lock:
        decisions = store._load_all()
        result = _reconcile(decisions, portfolio.trades, portfolio.realized_trades())
        if result.changed:
            store._rewrite(decisions)
        portfolio.persist_trade_metadata({
            decision.entry_trade_id: decision.id
            for decision in decisions
            if decision.entry_trade_id
        })
        return result


def _reconcile(
    decisions: list[TradeDecision],
    trades: list[PaperTrade],
    realized: list[RealizedTrade],
) -> ReconciliationResult:
    changed_ids: set[str] = set()
    created_ids: set[str] = set()
    decisions_by_id = {decision.id: decision for decision in decisions}
    linked_decision_ids = {
        decision.id for decision in decisions if decision.entry_trade_id
    }

    buy_trades = sorted(
        (trade for trade in trades if trade.side == "buy"),
        key=lambda trade: trade.timestamp,
    )

    for trade in buy_trades:
        if not trade.decision_id:
            continue
        decision = decisions_by_id.get(trade.decision_id)
        if decision is None:
            decision = _decision_from_trade(trade)
            decisions.append(decision)
            decisions_by_id[decision.id] = decision
            created_ids.add(decision.id)
            changed_ids.add(decision.id)
        before = asdict(decision)
        decision.entry_trade_id = trade.trade_id
        decision.reconciliation_status = decision.reconciliation_status or "open"
        if asdict(decision) != before:
            changed_ids.add(decision.id)
        linked_decision_ids.add(decision.id)

    claimed_trade_ids = {
        decision.entry_trade_id for decision in decisions if decision.entry_trade_id
    }
    for trade in buy_trades:
        if trade.trade_id in claimed_trade_ids or trade.decision_id:
            continue
        decision = _match_legacy_decision(trade, decisions, linked_decision_ids)
        if decision is None:
            continue
        decision.entry_trade_id = trade.trade_id
        decision.reconciliation_status = "open"
        linked_decision_ids.add(decision.id)
        claimed_trade_ids.add(trade.trade_id)
        changed_ids.add(decision.id)

    allocations_by_entry: dict[str, list[RealizedTrade]] = {}
    for allocation in realized:
        allocations_by_entry.setdefault(allocation.entry_trade_id, []).append(allocation)

    for decision in decisions:
        before = asdict(decision)
        if not decision.entry_trade_id:
            if decision.reconciliation_status is None:
                decision.reconciliation_status = "unmatched"
        else:
            allocations = allocations_by_entry.get(decision.entry_trade_id, [])
            _apply_allocations(decision, allocations)
        if asdict(decision) != before:
            changed_ids.add(decision.id)

    unmatched = sum(1 for decision in decisions if decision.reconciliation_status == "unmatched")
    matched = sum(1 for decision in decisions if decision.entry_trade_id)
    return ReconciliationResult(
        changed=len(changed_ids),
        created=len(created_ids),
        matched=matched,
        unmatched=unmatched,
    )


def _decision_from_trade(trade: PaperTrade) -> TradeDecision:
    return TradeDecision(
        id=str(trade.decision_id),
        symbol=trade.symbol,
        side="buy",
        quantity=trade.quantity,
        price=trade.price,
        account=_account_value(trade),
        reasoning=trade.reason,
        market_context="",
        decided_at=trade.timestamp.isoformat(),
        status="pending",
        entry_trade_id=trade.trade_id,
        reconciliation_status="open",
    )


def _match_legacy_decision(
    trade: PaperTrade,
    decisions: list[TradeDecision],
    linked_decision_ids: set[str],
) -> TradeDecision | None:
    candidates = [
        decision
        for decision in decisions
        if decision.id not in linked_decision_ids
        and decision.side == "buy"
        and decision.symbol == trade.symbol
        and decision.account == _account_value(trade)
        and decision.quantity == trade.quantity
        and abs(decision.price - trade.price) < 1e-9
    ]
    if not candidates:
        return None
    exact_reason = [decision for decision in candidates if decision.reasoning == trade.reason]
    pool = exact_reason or candidates
    return min(pool, key=lambda decision: _time_distance_seconds(decision.decided_at, trade.timestamp))


def _time_distance_seconds(value: str, trade_time: datetime) -> float:
    try:
        decision_time = datetime.fromisoformat(value)
        if decision_time.tzinfo is not None:
            decision_time = decision_time.replace(tzinfo=None)
        if trade_time.tzinfo is not None:
            trade_time = trade_time.replace(tzinfo=None)
        return abs((decision_time - trade_time).total_seconds())
    except (TypeError, ValueError):
        return float("inf")


def _apply_allocations(decision: TradeDecision, allocations: list[RealizedTrade]) -> None:
    old_status = decision.status
    old_pnl = decision.outcome_pnl_pct
    if not allocations:
        _set_open_outcome(decision)
        if old_status == "reflected":
            _archive_stale_reflection(decision)
        return

    resolved_quantity = sum(item.quantity for item in allocations)
    cost_basis = sum(item.entry_price * item.quantity for item in allocations)
    proceeds = sum(item.exit_price * item.quantity for item in allocations)
    net_pnl = sum(item.pnl for item in allocations)
    fees = sum(item.fees for item in allocations)
    outcome_price = proceeds / resolved_quantity
    gross_pnl_pct = (proceeds - cost_basis) / cost_basis * 100 if cost_basis else 0.0
    net_pnl_pct = net_pnl / cost_basis * 100 if cost_basis else 0.0
    fully_resolved = resolved_quantity >= decision.quantity
    desired_status = "resolved" if fully_resolved else "partial"
    outcome_changed = old_pnl is not None and abs(old_pnl - round(gross_pnl_pct, 2)) > 0.005

    decision.resolved_quantity = resolved_quantity
    decision.outcome_price = round(outcome_price, 6)
    decision.outcome_pnl_pct = round(gross_pnl_pct, 2)
    decision.outcome_cost_basis = round(cost_basis, 2)
    decision.outcome_proceeds = round(proceeds, 2)
    decision.outcome_fees = round(fees, 2)
    decision.outcome_net_pnl = round(net_pnl, 2)
    decision.outcome_net_pnl_pct = round(net_pnl_pct, 2)
    decision.outcome_trade_ids = list(dict.fromkeys(item.exit_trade_id for item in allocations))
    decision.outcome_source = "trade_ledger_fifo"
    decision.reconciliation_status = "confirmed" if fully_resolved else "partial"
    decision.holding_days = max((item.closed_at - item.opened_at).days for item in allocations)
    decision.resolved_at = max(item.closed_at for item in allocations).isoformat()

    if old_status == "reflected" and fully_resolved and not outcome_changed:
        decision.status = "reflected"
    else:
        if old_status == "reflected" and (outcome_changed or not fully_resolved):
            _archive_stale_reflection(decision)
        decision.status = desired_status


def _set_open_outcome(decision: TradeDecision) -> None:
    decision.status = "pending"
    decision.resolved_quantity = 0
    decision.outcome_price = None
    decision.outcome_pnl_pct = None
    decision.outcome_cost_basis = None
    decision.outcome_proceeds = None
    decision.outcome_fees = None
    decision.outcome_net_pnl = None
    decision.outcome_net_pnl_pct = None
    decision.outcome_trade_ids = []
    decision.outcome_source = None
    decision.reconciliation_status = "open"
    decision.holding_days = None
    decision.resolved_at = None


def _archive_stale_reflection(decision: TradeDecision) -> None:
    if decision.reflection and decision.reflection not in decision.reflection_history:
        decision.reflection_history.append(decision.reflection)
    decision.reflection = None
    decision.reflection_stale = True


def _account_value(trade: PaperTrade) -> str:
    return trade.account.value if hasattr(trade.account, "value") else str(trade.account)
