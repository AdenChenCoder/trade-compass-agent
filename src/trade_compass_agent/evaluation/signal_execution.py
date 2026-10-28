"""Explicit signal references and ledger-derived, account-aware outcomes."""

import json
from pathlib import Path

from trade_compass_agent.evaluation.research import ResearchStore


def validate_research_reference(data_dir: Path, data: dict) -> dict:
    keys = ("strategy_id", "strategy_version", "research_run_id")
    refs = {key: data.get(key) for key in keys}
    if bool(refs["strategy_id"]) != bool(refs["strategy_version"]):
        raise ValueError("strategy_id 与 strategy_version 必须同时提供")
    store = ResearchStore(data_dir)
    if refs["strategy_id"]:
        strategy = store.get_strategy(refs["strategy_id"], refs["strategy_version"])
        if strategy["definition"]["market"] != "CN":
            raise ValueError("当前模拟账户只能关联 A 股策略")
    if refs["research_run_id"]:
        run = store.get_run(refs["research_run_id"])
        if (run["strategy_id"], run["strategy_version"], run["symbol"]) != (
            refs["strategy_id"], refs["strategy_version"], data.get("symbol"),
        ):
            raise ValueError("研究记录与策略版本或标的不一致")
    return refs


def resolve_signal_reference(data_dir: Path, signal_id: str | None, symbol: str, account: str) -> dict:
    if not signal_id:
        return {}
    path = data_dir / "signals.jsonl"
    records = []
    for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        if line.strip():
            record = json.loads(line)
            if record.get("signal_id") == signal_id:
                records.append(record)
    if len(records) != 1:
        raise ValueError("信号不存在或 ID 不唯一")
    signal = records[0]
    if signal.get("symbol") != symbol or (signal.get("account") and signal["account"] != account):
        raise ValueError("信号的标的或账户与成交不一致")
    refs = validate_research_reference(data_dir, signal)
    return {"signal_id": signal_id, "source_skills": tuple(signal.get("source_skills") or []), **refs}


def reconcile_signal_executions(data_dir: Path, portfolio) -> int:
    """Derived tracking is rebuildable; never infer links for legacy fills."""
    from trade_compass_agent.evaluation.signal_tracker import SignalTracker

    tracker = SignalTracker(data_dir)
    changed = 0
    from trade_compass_agent.concurrency import file_transaction
    from trade_compass_agent.portfolio import JsonPaperPortfolio
    with file_transaction(tracker.path.with_suffix(".lock")), tracker._lock:
        # A caller may have an older snapshot after concurrent fills. Always
        # read the authoritative ledger after acquiring the tracking lock.
        portfolio = JsonPaperPortfolio(data_dir / "paper_trades.jsonl", costs=portfolio.costs)
        allocations = portfolio.realized_trades()
        records = tracker._load_all()
        for record in records:
            buys = [trade for trade in portfolio.trades if trade.side == "buy" and trade.signal_id == record.signal_id]
            if not buys:
                continue
            accounts = {trade.account.value for trade in buys}
            if len(accounts) != 1 or (record.account and record.account not in accounts):
                record.attribution_status = "ambiguous_accounts"
                record.status = "pending"
                record.actual_entry = record.actual_exit = record.actual_pnl = None
                record.net_pnl = record.fees = None
                record.outcome = ""
                record.closed_at = None
                changed += 1
                continue
            record.account = next(iter(accounts))
            record.entry_trade_ids = [trade.trade_id for trade in buys]
            fills = [fill for fill in allocations if fill.entry_trade_id in record.entry_trade_ids]
            record.exit_trade_ids = list(dict.fromkeys(fill.exit_trade_id for fill in fills))
            record.entry_quantity = sum(trade.quantity for trade in buys)
            record.resolved_quantity = sum(fill.quantity for fill in fills)
            record.actual_entry = sum(trade.quantity * trade.price for trade in buys) / record.entry_quantity
            record.net_pnl = round(sum(fill.pnl for fill in fills), 4)
            record.fees = round(sum(fill.fees for fill in fills), 4)
            entry_fee_by_id = {trade.trade_id: portfolio.estimate_fee(trade) / trade.quantity for trade in buys}
            basis = sum(fill.quantity * (fill.entry_price + entry_fee_by_id[fill.entry_trade_id]) for fill in fills)
            record.actual_pnl = round(record.net_pnl / basis * 100, 4) if basis else None
            record.actual_exit = (sum(fill.exit_price * fill.quantity for fill in fills) / record.resolved_quantity
                                  if record.resolved_quantity else None)
            record.status = "closed" if record.resolved_quantity == record.entry_quantity else "active"
            record.outcome = ("win" if record.net_pnl > 0 else "loss" if record.net_pnl < 0 else "breakeven") if record.status == "closed" else ""
            record.closed_at = max(fill.closed_at for fill in fills).isoformat() if record.status == "closed" else None
            if record.closed_at:
                record.days_held = (max(fill.closed_at for fill in fills).date() - min(trade.timestamp for trade in buys).date()).days
            record.attribution_status = "explicit_ledger"
            record.outcome_basis = "execution_net_pnl"
            changed += 1
        if changed:
            tracker._save_all(records)
    return changed
