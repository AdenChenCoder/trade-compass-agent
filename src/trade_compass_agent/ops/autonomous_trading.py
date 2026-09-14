"""Clock and activation gate for the intraday paper-trading workflow."""

from datetime import datetime
from pathlib import Path

from trade_compass_agent.portfolio.trading_policy import AutonomousTradingStore, TradeRejected

JOB_ID = "autonomous_trading"
SCHEDULE = "trading_session 30m"
SESSION_PREFIX = "scheduler-autonomous_trading-"
RUN_TIMES = ((9, 35), (10, 5), (10, 35), (11, 5),
             (13, 5), (13, 35), (14, 5), (14, 35))


def latest_slot(now: datetime) -> datetime | None:
    slots = [now.replace(hour=h, minute=m, second=0, microsecond=0) for h, m in RUN_TIMES]
    return next((slot for slot in reversed(slots) if slot <= now), None)


def skip_reason(data_dir: Path) -> str | None:
    if not AutonomousTradingStore(data_dir).read():
        return "Agent 自主交易已关闭"
    # Reuse the order tool's market calendar and Shanghai trading-session gate.
    from trade_compass_agent.runtime.tools.portfolio import _market_now, _validate_quote_time

    now = _market_now()
    try:
        _validate_quote_time(now, now)
    except TradeRejected as exc:
        return str(exc)
    return None
