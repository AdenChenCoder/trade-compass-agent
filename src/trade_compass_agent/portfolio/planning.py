"""Dated previous-close references for premarket planning, never execution quotes."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, time
import math
from zoneinfo import ZoneInfo

from trade_compass_agent.ops.trading_calendar import is_trading_day, prev_trading_day

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def premarket_reference_date(run_date: date, *, now: datetime | None = None) -> date | None:
    now = now or datetime.now(_SHANGHAI)
    now = now.replace(tzinfo=_SHANGHAI) if now.tzinfo is None else now.astimezone(_SHANGHAI)
    if now.date() != run_date or now.time() >= time(9, 30) or not is_trading_day(run_date):
        return None
    return prev_trading_day(run_date)


def planning_close_references(provider, symbols, reference_date: date, *, timeout: float = 8.0) -> dict:
    """Require the exact previous trading session and real provider provenance."""
    symbols = set(symbols)
    if not symbols:
        return {}

    def fetch(symbol):
        try:
            bars = provider.get_bars(symbol, timeframe="1d", limit=3)
            for bar in reversed(bars):
                observed = bar.timestamp
                if observed.tzinfo is not None:
                    observed = observed.astimezone(_SHANGHAI)
                if (bar.symbol != symbol or observed.date() != reference_date
                        or not bar.source or bar.source.lower() in {"sample", "synthetic", "mock"}
                        or not math.isfinite(bar.close) or bar.close <= 0):
                    continue
                return {"date": reference_date.isoformat(), "close": bar.close,
                        "source": bar.source, "adjusted": bar.adjusted,
                        "purpose": "planning_only"}
        except Exception:
            return None
        return None

    pool = ThreadPoolExecutor(max_workers=min(8, len(symbols)))
    futures = {pool.submit(fetch, symbol): symbol for symbol in symbols}
    try:
        done, pending = wait(futures, timeout=timeout)
        for future in pending:
            future.cancel()
        return {futures[future]: value for future in done if (value := future.result()) is not None}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
