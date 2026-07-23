"""Health check and operational metrics for the Trade Compass Agent."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

_START_TIME = time.time()


def build_health_report() -> dict[str, Any]:
    """Lightweight health check — fast, no heavy I/O."""
    checks: dict[str, str] = {}

    checks["llm"] = _check_llm_key()
    checks["data_dir"] = _check_data_dir()
    checks["scheduler"] = _check_scheduler()

    _healthy = {"ok", "enabled", "disabled"}
    overall = "ok" if all(v in _healthy for v in checks.values()) else "degraded"

    return {
        "status": overall,
        "uptime_seconds": round(time.time() - _START_TIME, 1),
        "checks": checks,
    }


def build_metrics() -> dict[str, Any]:
    """Detailed operational metrics — may perform light file reads."""
    from trade_compass_agent.config import load_app_config

    config = load_app_config()
    data_dir = config.data_dir

    metrics: dict[str, Any] = {
        "uptime_seconds": round(time.time() - _START_TIME, 1),
    }

    metrics["signals"] = _signal_metrics(data_dir)
    metrics["portfolio"] = _portfolio_metrics(config)
    metrics["scheduler"] = _scheduler_metrics(config)
    metrics["jobs"] = _job_metrics(data_dir)

    return metrics


def _check_llm_key() -> str:
    has_key = any(
        os.getenv(k)
        for k in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DASHSCOPE_API_KEY")
    )
    return "ok" if has_key else "no_key"


def _check_data_dir() -> str:
    from trade_compass_agent.config import load_app_config
    try:
        config = load_app_config()
        return "ok" if config.data_dir.exists() else "missing"
    except Exception:
        return "error"


def _check_scheduler() -> str:
    if os.getenv("TRADE_COMPASS_NO_SCHEDULER", "").lower() in {"1", "true", "yes"}:
        return "disabled"
    from trade_compass_agent.config import load_app_config

    try:
        config = load_app_config()
        return "enabled" if config.scheduler.enabled else "disabled"
    except Exception:
        return "unknown"


def _signal_metrics(data_dir) -> dict[str, Any]:
    signals_dir = data_dir / "signals"
    if not signals_dir.exists():
        return {"total": 0, "pending": 0, "active": 0, "closed": 0}

    tracker_path = data_dir / "signal_tracker.json"
    if not tracker_path.exists():
        signal_files = list(signals_dir.glob("*.json"))
        return {"total": len(signal_files), "pending": 0, "active": 0, "closed": 0}

    try:
        import json
        data = json.loads(tracker_path.read_text())
        signals = data.get("signals", {})
        statuses = [s.get("status", "unknown") for s in signals.values()]
        return {
            "total": len(statuses),
            "pending": statuses.count("pending"),
            "active": statuses.count("active"),
            "closed": statuses.count("closed"),
        }
    except Exception:
        return {"total": 0, "error": "parse_failed"}


def _portfolio_metrics(config) -> dict[str, Any]:
    from trade_compass_agent.portfolio import JsonPaperPortfolio
    try:
        portfolio = JsonPaperPortfolio(
            config.data_dir / "paper_trades.jsonl",
            costs=config.trading_costs,
        )
        positions = portfolio.positions()
        realized = portfolio.realized_trades()
        wins = sum(1 for t in realized if t.pnl > 0)
        total_pnl = sum(t.pnl for t in realized)
        return {
            "open_positions": len(positions),
            "total_trades": len(portfolio.trades),
            "realized_count": len(realized),
            "win_rate": round(wins / len(realized), 3) if realized else 0.0,
            "total_pnl": round(total_pnl, 2),
        }
    except Exception:
        return {"open_positions": 0, "total_trades": 0}


def _scheduler_metrics(config) -> dict[str, Any]:
    return {
        "enabled": config.scheduler.enabled,
        "timezone": config.scheduler.timezone,
        "premarket_time": config.scheduler.premarket_time,
        "morning_plan_time": config.scheduler.morning_plan_time,
        "close_time": config.scheduler.close_time,
        "eod_review_time": config.scheduler.eod_review_time,
        "postmarket_time": config.scheduler.postmarket_time,
        "weekly_day": config.scheduler.weekly_day,
        "weekly_time": config.scheduler.weekly_time,
    }


def _job_metrics(data_dir) -> dict[str, Any]:
    try:
        from trade_compass_agent.ops.run_store import SqliteRunStore
        store = SqliteRunStore(data_dir / "scheduler.db")
        runs = store.recent_runs(limit=1)
        last = runs[0] if runs else None
        return {
            "last_run": {
                "job_id": last.job_id,
                "status": last.status,
                "finished_at": last.finished_at.isoformat() if last.finished_at else None,
            } if last else None,
        }
    except Exception:
        return {"last_run": None}
