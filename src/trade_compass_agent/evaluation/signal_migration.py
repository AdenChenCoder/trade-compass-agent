"""Preview-first historical signal tracking cleanup and 1-to-1 fill migration."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from trade_compass_agent.concurrency import atomic_write, file_transaction
from trade_compass_agent.evaluation.signal_execution import reconcile_signal_executions
from trade_compass_agent.evaluation.signal_tracker import ENTRY_SIGNAL_RATINGS, SignalTracker
from trade_compass_agent.portfolio import JsonPaperPortfolio
from trade_compass_agent.portfolio.simulator import _trade_payload
from trade_compass_agent.portfolio.trading_policy import portfolio_transaction

_CST = ZoneInfo("Asia/Shanghai")


def _to_cst_naive(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(_CST).replace(tzinfo=None) if value.tzinfo is not None else value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(_CST).replace(tzinfo=None) if parsed.tzinfo is not None else parsed
    except ValueError:
        return None


def _backup_files(data_dir: Path, files: list[Path]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = data_dir / "recovery" / f"signal-migration-{stamp}-{uuid4().hex[:8]}"
    backup_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    for src in files:
        if src.exists():
            dst = backup_dir / src.name
            shutil.copy2(src, dst)
            os.chmod(dst, 0o600)
    return backup_dir


def reconcile_historical_signals(
    data_dir: Path,
    *,
    costs: Any = None,
    apply: bool = False,
    window_days: int = 5,
    max_trading_days: int = 5,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Preview (default) or apply strict 1-to-1 historical buy linkage and pending cleanup.

    Never infers ambiguous links (1-to-many or many-to-1). Applied historical links
    carry ``attribution_status = 'migrated_historical'`` distinct from live ``explicit_ledger``.
    """
    from trade_compass_agent.ops.trading_calendar import is_trading_day

    target_day = as_of or datetime.now(_CST).date()
    tracker = SignalTracker(data_dir)
    ledger_path = data_dir / "paper_trades.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    tracker.path.parent.mkdir(parents=True, exist_ok=True)

    lock_cm_ledger = portfolio_transaction(ledger_path) if apply else nullcontext()
    lock_cm_tracker = file_transaction(tracker.path.with_suffix(".lock")) if apply else nullcontext()
    mem_lock_tracker = tracker._lock if apply else nullcontext()

    backup_dir: Path | None = None
    with lock_cm_ledger, lock_cm_tracker, mem_lock_tracker:
        portfolio = (
            JsonPaperPortfolio(ledger_path, costs=costs)
            if costs is not None
            else JsonPaperPortfolio(ledger_path)
        )
        records = tracker._load_all()

        non_entry_pending = [
            r for r in records
            if r.status == "pending" and not r.entry_trade_ids and r.rating.lower() not in ENTRY_SIGNAL_RATINGS
        ]
        entry_candidates = [
            r for r in records
            if r.status == "pending" and not r.entry_trade_ids and r.rating.lower() in ENTRY_SIGNAL_RATINGS
        ]
        unlinked_buys = [t for t in portfolio.trades if t.side == "buy" and not t.signal_id]

        trade_to_signals: dict[str, list[str]] = {}
        signal_to_trades: dict[str, list[str]] = {}
        signal_by_id = {r.signal_id: r for r in entry_candidates if r.signal_id}

        for trade in unlinked_buys:
            acct_str = trade.account.value if hasattr(trade.account, "value") else str(trade.account)
            trade_dt = _to_cst_naive(trade.timestamp)
            if trade_dt is None:
                continue
            for sig in entry_candidates:
                if not sig.signal_id or sig.symbol != trade.symbol:
                    continue
                if sig.account and sig.account != acct_str:
                    continue
                sig_dt = _to_cst_naive(sig.emitted_at)
                if sig_dt is None:
                    continue
                day_diff = (trade_dt.date() - sig_dt.date()).days
                if 0 <= day_diff <= window_days and sig_dt <= trade_dt:
                    trade_to_signals.setdefault(trade.trade_id, []).append(sig.signal_id)
                    signal_to_trades.setdefault(sig.signal_id, []).append(trade.trade_id)

        unambiguous_pairs: list[dict[str, Any]] = []
        ambiguous_trades: list[dict[str, Any]] = []
        matched_signal_ids: set[str] = set()
        matched_trade_to_signal: dict[str, str] = {}

        for trade in unlinked_buys:
            candidates = trade_to_signals.get(trade.trade_id, [])
            if not candidates:
                continue
            if len(candidates) == 1 and len(signal_to_trades.get(candidates[0], [])) == 1:
                sig = signal_by_id[candidates[0]]
                matched_signal_ids.add(sig.signal_id)
                matched_trade_to_signal[trade.trade_id] = sig.signal_id
                unambiguous_pairs.append(
                    {
                        "trade_id": trade.trade_id,
                        "symbol": trade.symbol,
                        "account": trade.account.value if hasattr(trade.account, "value") else str(trade.account),
                        "trade_timestamp": trade.timestamp.isoformat(),
                        "quantity": trade.quantity,
                        "price": trade.price,
                        "signal_id": sig.signal_id,
                        "signal_rating": sig.rating,
                        "signal_emitted_at": sig.emitted_at,
                    }
                )
            else:
                ambiguous_trades.append(
                    {
                        "trade_id": trade.trade_id,
                        "symbol": trade.symbol,
                        "trade_timestamp": trade.timestamp.isoformat(),
                        "candidate_signal_ids": candidates,
                    }
                )

        would_expire_count = 0
        for r in entry_candidates:
            if r.signal_id in matched_signal_ids:
                continue
            sig_dt = _to_cst_naive(r.emitted_at)
            if sig_dt is None or sig_dt.date() >= target_day:
                continue
            emitted_day = sig_dt.date()
            if (target_day - emitted_day).days > max_trading_days * 3:
                would_expire_count += 1
                continue
            elapsed = 0
            cursor = emitted_day + timedelta(days=1)
            while cursor <= target_day:
                if is_trading_day(cursor):
                    elapsed += 1
                cursor += timedelta(days=1)
            if elapsed > max_trading_days:
                would_expire_count += 1

        summary: dict[str, Any] = {
            "mode": "applied" if apply else "preview",
            "as_of": target_day.isoformat(),
            "window_days": window_days,
            "total_tracking_records": len(records),
            "total_unlinked_buys": len(unlinked_buys),
            "non_entry_pending_to_observed": len(non_entry_pending),
            "unambiguous_matches_count": len(unambiguous_pairs),
            "ambiguous_skipped_count": len(ambiguous_trades),
            "stale_buy_pending_to_expired": would_expire_count,
            "unambiguous_matches": unambiguous_pairs,
            "ambiguous_skipped": ambiguous_trades[:25],
            "backup_dir": None,
        }

        if not apply:
            return summary

        backup_dir = _backup_files(
            data_dir,
            [ledger_path, tracker.path, data_dir / "decisions.jsonl"],
        )
        summary["backup_dir"] = str(backup_dir)

        if matched_trade_to_signal and ledger_path.exists():
            rewritten_lines: list[str] = []
            for t in portfolio.trades:
                sig_id = matched_trade_to_signal.get(t.trade_id)
                if sig_id and not t.signal_id:
                    sig_obj = signal_by_id[sig_id]
                    t = replace(
                        t,
                        signal_id=sig_id,
                        source_skills=tuple(sig_obj.source_skills or ()),
                        strategy_id=sig_obj.strategy_id,
                        strategy_version=sig_obj.strategy_version,
                        research_run_id=sig_obj.research_run_id,
                    )
                    payload = {**_trade_payload(t), "signal_attribution": "migrated_historical"}
                else:
                    payload = _trade_payload(t)
                rewritten_lines.append(json.dumps(payload, ensure_ascii=False))
            atomic_write(ledger_path, "\n".join(rewritten_lines) + ("\n" if rewritten_lines else ""))

        changed_meta = False
        for r in records:
            if r.signal_id in matched_signal_ids:
                r.attribution_status = "migrated_historical"
                changed_meta = True
            elif r.status == "pending" and not r.entry_trade_ids and r.rating.lower() not in ENTRY_SIGNAL_RATINGS:
                r.status = "observed"
                changed_meta = True
        if changed_meta:
            tracker._save_all(records)

    refreshed_portfolio = (
        JsonPaperPortfolio(ledger_path, costs=costs)
        if costs is not None
        else JsonPaperPortfolio(ledger_path)
    )
    reconcile_signal_executions(data_dir, refreshed_portfolio)
    expired_applied = tracker.expire_pending(as_of=target_day, max_trading_days=max_trading_days)
    summary["expired_applied"] = expired_applied
    return summary
