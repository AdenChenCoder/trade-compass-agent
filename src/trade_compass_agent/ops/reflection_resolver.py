"""Market-aware resolve functions for Deferred Reflection."""

from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo
import math
from typing import Any

from trade_compass_agent.config import AppConfig
from trade_compass_agent.ops.reflection import JobReflection, PendingReflection

logger = logging.getLogger(__name__)

BUILTIN_JOB_IDS = (
    "premarket",
    "morning_plan",
    "close",
    "eod_review",
    "postmarket",
    "weekly",
)

_POSITION_STEP_KEYS = (
    "mark_to_market",
    "pnl_review",
    "portfolio_scan",
    "portfolio_check",
)

_ALERT_STEP_KEYS = ("exit_check", "portfolio_scan")


def _position_account(pos: Any) -> str | None:
    acct = getattr(pos, "account", None)
    if isinstance(acct, str):
        return acct
    val = getattr(acct, "value", None)
    return val if isinstance(val, str) else None


def _step_sources(predictions: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = [predictions]
    workflow = predictions.get("workflow")
    if isinstance(workflow, dict):
        snapshots = workflow.get("step_snapshots")
        if isinstance(snapshots, dict):
            sources.append(snapshots)
        sources.append(workflow)
    return sources


def _has_recorded_empty_positions(predictions: dict[str, Any]) -> bool:
    for source in _step_sources(predictions):
        for key in _POSITION_STEP_KEYS:
            step = source.get(key)
            if isinstance(step, dict) and step.get("positions") == []:
                return True
    return False


def extract_position_snapshots(predictions: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull position list from known step outputs (direct or nested under workflow)."""
    for source in _step_sources(predictions):
        for key in _POSITION_STEP_KEYS:
            step = source.get(key)
            positions = step.get("positions") if isinstance(step, dict) else None
            if positions:
                return list(positions)
    return []


def extract_alerts(predictions: dict[str, Any]) -> list[str]:
    alerts: list[str] = []
    for source in _step_sources(predictions):
        for key in _ALERT_STEP_KEYS:
            step = source.get(key)
            if not isinstance(step, dict):
                continue
            for alert in step.get("alerts", []):
                if alert not in alerts:
                    alerts.append(alert)
    return alerts


def resolve_pending_with_market(
    pending: PendingReflection,
    config: AppConfig,
    *,
    as_of: date | None = None,
) -> tuple[dict[str, Any], str] | None:
    """Compare stored position snapshots against current portfolio.

    Returns None to keep pending when run_date is today or later (need elapsed time).
    """
    today = as_of or datetime.now(ZoneInfo(config.scheduler.timezone)).date()
    try:
        run_date = date.fromisoformat(pending.run_date)
    except ValueError:
        run_date = today

    if run_date >= today:
        return None

    predictions = pending.predictions
    positions = extract_position_snapshots(predictions)
    if not positions:
        suffix = "（当日空仓）" if _has_recorded_empty_positions(predictions) else "（无持仓快照）"
        return {}, f"[{pending.job_id} {pending.run_date}] {pending.summary}{suffix}"

    eligible_snapshots: list[dict[str, Any]] = []
    for snap in positions:
        symbol = snap.get("symbol")
        if not symbol or snap.get("price_is_fresh") is False:
            continue
        baseline = snap.get("pnl_pct", 0)
        if not isinstance(baseline, (int, float)) or isinstance(baseline, bool) or not math.isfinite(baseline):
            continue
        eligible_snapshots.append(snap)

    if not eligible_snapshots:
        return {}, f"[{pending.job_id} {pending.run_date}] {pending.summary}（盘前无实时估值基线）"

    from trade_compass_agent.portfolio import JsonPaperPortfolio
    from trade_compass_agent.runtime.market_stack import MarketStack

    portfolio = JsonPaperPortfolio(
        config.data_dir / "paper_trades.jsonl",
        costs=config.trading_costs,
    )
    stack = MarketStack.from_config(config)
    current_positions = portfolio.positions_with_market_prices(stack.provider)

    comparisons: list[str] = []
    actuals: dict[str, Any] = {"positions": [], "alerts": extract_alerts(predictions)}
    deferred_for_fresh_quote = False

    for snap in eligible_snapshots:
        symbol = snap["symbol"]
        predicted_pnl = float(snap.get("pnl_pct", 0))
        acct_filter = snap.get("account")
        matches = [
            p for p in current_positions
            if p.symbol == symbol
            and (not acct_filter or _position_account(p) == acct_filter)
        ]
        if len(matches) > 1:
            continue
        held = matches[0] if matches else None
        if held is None:
            actuals["positions"].append({
                "symbol": symbol,
                "predicted_pnl_pct": round(predicted_pnl, 2),
                "status": "closed",
            })
            comparisons.append(f"{symbol} 预测{predicted_pnl:+.1f}%→已平仓")
            continue

        if getattr(held, "price_is_fresh", True) is False:
            deferred_for_fresh_quote = True
            continue
        actual_pnl = (
            (held.last_price / held.avg_cost - 1) * 100
            if held.avg_cost > 0
            else 0.0
        )
        delta = actual_pnl - predicted_pnl
        entry = {
            "symbol": symbol,
            "predicted_pnl_pct": round(predicted_pnl, 2),
            "actual_pnl_pct": round(actual_pnl, 2),
            "delta_pnl_pct": round(delta, 2),
        }
        acct_name = _position_account(held)
        if acct_name:
            entry["account"] = acct_name
        actuals["positions"].append(entry)
        if abs(delta) >= 2.0:
            comparisons.append(f"{symbol} {predicted_pnl:+.1f}%→{actual_pnl:+.1f}% ({delta:+.1f}%)")

    if not actuals["positions"]:
        if deferred_for_fresh_quote and (today - run_date).days <= 5:
            return None
        return {}, f"[{pending.job_id} {pending.run_date}] {pending.summary}（现价未刷新，已归档）"

    if comparisons:
        shown = comparisons[:6]
        lesson = f"[{pending.job_id} {pending.run_date}] " + "; ".join(shown)
        if len(comparisons) > 6:
            lesson += f" 等{len(comparisons)}只"
    else:
        lesson = f"[{pending.job_id} {pending.run_date}] {pending.summary}（持仓变动<2%）"

    return actuals, lesson


def make_market_resolve_fn(config: AppConfig, *, as_of: date | None = None):
    """Return a resolve_fn bound to config for JobReflection.resolve_pending."""

    def _resolve(pending: PendingReflection) -> tuple[dict[str, Any], str] | None:
        return resolve_pending_with_market(pending, config, as_of=as_of)

    return _resolve


def resolve_all_job_reflections(
    memory_dir,
    config: AppConfig,
    *,
    job_ids: tuple[str, ...] = BUILTIN_JOB_IDS,
    as_of: date | None = None,
) -> dict[str, list]:
    """Resolve pending reflections for all built-in jobs."""
    from trade_compass_agent.memory.memory_store import MemoryStore

    reflection = JobReflection(memory_dir)
    mem_store = MemoryStore(memory_dir)
    resolve_fn = make_market_resolve_fn(config, as_of=as_of)
    results: dict[str, list] = {}
    for job_id in job_ids:
        resolved = reflection.resolve_pending(
            job_id,
            resolve_fn=resolve_fn,
            mem_store=mem_store,
            config=config,
        )
        if resolved:
            results[job_id] = resolved
            logger.info("Resolved %d reflections for %s", len(resolved), job_id)
    return results
