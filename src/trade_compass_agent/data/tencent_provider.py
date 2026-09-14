"""Tencent's public chart data, also used by AKShare's Tencent adapter."""
from __future__ import annotations

from datetime import datetime
import math
import threading
import time

import requests

from trade_compass_agent.domain import Bar, Instrument
from .providers import (
    ALL_TIMEFRAMES, DEFAULT_REQUEST_TIMEOUT, ProviderError,
    infer_instrument_kind, is_index_symbol, to_sina_code,
)


class TencentProvider:
    name = "tencent"
    supported_timeframes = ALL_TIMEFRAMES
    _slots = threading.BoundedSemaphore(4)
    _lock = threading.Lock()
    _next_request = 0.0
    _cooldown_until = 0.0

    def __init__(self, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> None:
        self.timeout = timeout

    def get_instrument(self, symbol: str) -> Instrument:
        return Instrument(symbol=symbol, name=symbol, kind=infer_instrument_kind(symbol))

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 120) -> list[Bar]:
        if timeframe not in self.supported_timeframes:
            raise ProviderError(f"unsupported timeframe: {timeframe}")
        code = to_sina_code(symbol)
        daily = timeframe == "1d"
        period = "day" if daily else "m" + timeframe[:-1]
        count = max(1, min(limit, 640 if daily else 320))
        if daily:
            url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
            params = {"param": f"{code},day,,,{count},qfq"}
        else:
            url = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
            params = {"param": f"{code},{period},,{count}"}

        # A bounded queue and pacing prevent the screener from bursting 40 calls.
        deadline = time.monotonic() + self.timeout
        if not self._slots.acquire(timeout=self.timeout):
            raise ProviderError("Tencent request queue timeout")
        try:
            cls = type(self)
            with cls._lock:
                now = time.monotonic()
                if now < cls._cooldown_until:
                    raise ProviderError("Tencent rate limited; retry after cooldown")
                scheduled = max(now, cls._next_request)
                if scheduled >= deadline:
                    raise ProviderError("Tencent request queue timeout")
                cls._next_request = scheduled + 0.05
            time.sleep(max(0, scheduled - time.monotonic()))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError("Tencent request queue timeout")
            response = requests.get(url, params=params, timeout=remaining)
            if response.status_code in {403, 429, 456}:
                with cls._lock:
                    cls._cooldown_until = time.monotonic() + 60
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0:
                raise ProviderError(f"Tencent data error: {payload.get('code')}")
            data = (payload.get("data") or {}).get(code) or {}
            key = "qfqday" if daily and data.get("qfqday") else period
            rows = data.get(key) or []
            # An unadjusted 'day' series must not be labelled as forward-adjusted.
            adjusted = daily and key == "qfqday" and not is_index_symbol(symbol)
            bars = [self._bar(symbol, row, daily=daily, adjusted=adjusted) for row in rows]
            if not bars:
                raise ProviderError(f"Tencent returned no {timeframe} bars for {symbol}")
            bars.sort(key=lambda bar: bar.timestamp)
            if len({bar.timestamp for bar in bars}) != len(bars):
                raise ProviderError("Tencent returned duplicate timestamps")
            return bars[-count:]
        except (KeyError, IndexError, TypeError, ValueError, requests.RequestException) as exc:
            raise ProviderError(f"Tencent bars failed for {symbol}: {exc}") from exc
        finally:
            self._slots.release()

    @staticmethod
    def _bar(symbol: str, row: list, *, daily: bool, adjusted: bool) -> Bar:
        timestamp = datetime.strptime(row[0], "%Y-%m-%d" if daily else "%Y%m%d%H%M")
        if daily:
            timestamp = timestamp.replace(hour=15)
        opening, close, high, low, lots = map(float, row[1:6])
        if not all(math.isfinite(v) for v in (opening, close, high, low, lots)):
            raise ProviderError("Tencent returned non-finite OHLCV")
        if min(opening, close, high, low) <= 0 or lots < 0 or not low <= min(opening, close) <= max(opening, close) <= high:
            raise ProviderError("Tencent returned invalid OHLCV")
        # Daily row[8] is turnover in CNY 10,000; minute row[7] is NOT money.
        amount = float(row[8]) * 10000 if daily and len(row) > 8 and row[8] not in (None, "") else None
        if amount is not None and (not math.isfinite(amount) or amount < 0):
            raise ProviderError("Tencent returned invalid turnover")
        turnover = float(row[7]) if daily and len(row) > 7 and row[7] not in (None, "") else None
        return Bar(
            symbol=symbol, timestamp=timestamp, open=opening, high=high, low=low,
            close=close, volume=lots * 100, amount=amount, adjusted=adjusted,
            turnover_pct=turnover, source="tencent",
        )
