from __future__ import annotations

import os
import math
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

from trade_compass_agent.domain import Bar, Instrument

from .network import rate_limit_domain, run_with_timeout, short_error_message
from .providers import (
    DEFAULT_REQUEST_TIMEOUT,
    MINUTE_TIMEFRAMES,
    ProviderError,
    _date_window,
    _market_now,
    _prev_trading_date,
    infer_instrument_kind,
    split_symbol,
    is_index_symbol,
    is_lof_symbol,
)

_MAX_ROWS = 6000
_BAR_FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount"


def query_tushare(api_name: str, *, token: str, timeout: float, fields: str = "", **params):
    """Use the official HTTPS API for both bars and fundamentals.

    SDK versions use different URL paths and can turn HTTP errors into empty
    frames. Check transport and business failures before accepting any data.
    """
    url = "https://api.tushare.pro"
    deadline = time.monotonic() + timeout
    try:
        rate_limit_domain(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError("Tushare request queue timeout")
        response = requests.post(
            url, json={"api_name": api_name, "token": token, "params": params, "fields": fields},
            timeout=remaining, allow_redirects=False,
        )
        if response.status_code != 200:
            raise ProviderError(f"Tushare HTTP {response.status_code}")
        payload = response.json()
        if payload.get("code") != 0:
            raise ProviderError(f"Tushare {payload.get('code')}: {payload.get('msg') or 'request failed'}")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("fields"), list) or not isinstance(data.get("items"), list):
            raise ProviderError("Tushare returned invalid data")
        return pd.DataFrame(data["items"], columns=data["fields"])
    except Exception as exc:
        message = str(exc).replace(token, "[redacted]")
        raise ProviderError(short_error_message(RuntimeError(message))) from None


def to_ts_code(symbol: str) -> str:
    """Map A-share symbol to Tushare ts_code (e.g. 600519 -> 600519.SH)."""

    market, code = split_symbol(symbol)
    return f"{code}.{market.upper()}"


def _tushare_rows_to_bars(symbol: str, df, limit: int) -> list[Bar]:
    if df is None or getattr(df, "empty", True):
        raise ProviderError(f"no bars for {symbol}")

    df = df.sort_values("trade_date").tail(limit)
    bars: list[Bar] = []
    now = _market_now()
    latest_allowed = _prev_trading_date(now.date(), now.hour)
    for _, row in df.iterrows():
        if row.get("ts_code") != to_ts_code(symbol):
            raise ProviderError(f"Tushare returned a different security for {symbol}")
        date_value = row.get("trade_date") or row.get("date")
        timestamp = datetime.strptime(str(date_value)[:8], "%Y%m%d")
        if timestamp.weekday() >= 5 or timestamp.date() > latest_allowed:
            raise ProviderError("Tushare returned a non-trading or unclosed daily bar")
        opening, high, low, close, volume = (float(row[key]) for key in ("open", "high", "low", "close", "vol"))
        amount = row.get("amount")
        amount = None if amount is None or pd.isna(amount) else float(amount) * 1000
        if not all(math.isfinite(value) for value in (opening, high, low, close, volume)):
            raise ProviderError("Tushare returned non-finite OHLCV")
        if not 0 < low <= min(opening, close) <= max(opening, close) <= high or volume < 0:
            raise ProviderError("Tushare returned invalid OHLCV")
        if amount is not None and (not math.isfinite(amount) or amount < 0):
            raise ProviderError("Tushare returned invalid amount")
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=timestamp.replace(hour=15, minute=0, second=0, microsecond=0),
                open=opening,
                high=high,
                low=low,
                close=close,
                volume=volume * 100,
                amount=amount,
                adjusted=False,
                source="tushare",
            )
        )
    if not bars:
        raise ProviderError(f"no bars for {symbol}")
    if len({bar.timestamp for bar in bars}) != len(bars):
        raise ProviderError("Tushare returned duplicate dates")
    return bars


class TushareProvider:
    """Optional Tushare Pro daily bars (token-gated)."""

    name = "tushare"
    supported_timeframes = {"1d"}
    closed_daily_only = True

    def __init__(
        self,
        *,
        token: str | None = None,
        token_env: str = "TUSHARE_TOKEN",
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self.token_env = token_env
        self._token = (token or os.getenv(token_env, "")).strip()
        if not self._token:
            raise ProviderError(
                f"Tushare token missing; set env {token_env}"
            )
        self.timeout = timeout

    def get_instrument(self, symbol: str) -> Instrument:
        from trade_compass_agent.domain import InstrumentKind
        kind = InstrumentKind.INDEX if is_index_symbol(symbol) else infer_instrument_kind(symbol)
        return Instrument(symbol=symbol, name=symbol, kind=kind, exchange="TUSHARE")

    def get_bars_batch(
        self, symbols: list[str], *, limit: int, timeout: float,
    ) -> tuple[dict[str, list[Bar]], dict[str, str]]:
        """Batch history within one budget, preserving fund/index endpoint routing.

        Large universes use daily cross-sections as recommended by Tushare.
        Short histories are checked against the normal single-symbol window.
        Incomplete date downloads never become apparently complete cache entries.
        """
        from trade_compass_agent.domain import InstrumentKind
        from trade_compass_agent.ops.trading_calendar import is_trading_day

        if not 1 <= limit <= _MAX_ROWS:
            raise ProviderError("invalid Tushare daily batch limit")
        codes = {to_ts_code(s): s for s in symbols if not is_index_symbol(s)
                 and not is_lof_symbol(s) and infer_instrument_kind(s) != InstrumentKind.ETF}
        stock_symbols = set(codes.values())
        others = [s for s in dict.fromkeys(symbols) if s not in stock_symbols]
        if not codes:
            return self._get_individual_batch(others, limit=limit, timeout=timeout)
        deadline = time.monotonic() + timeout
        start, _ = _date_window(limit * 2)
        now = _market_now()
        end = _prev_trading_date(now.date(), now.hour)
        window_days = max(1, (end - datetime.strptime(start, "%Y%m%d").date()).days + 1)
        chunk_size = max(1, _MAX_ROWS // window_days)
        frames: dict[str, object] = {}
        results: dict[str, list[Bar]] = {}
        errors: dict[str, str] = {}
        complete: set[str] = set()

        def query(**params):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError("Tushare batch time budget exhausted")
            call_timeout = min(remaining, max(self.timeout, 6.0))
            frame = run_with_timeout(
                lambda: query_tushare("daily", token=self._token, timeout=call_timeout,
                                      fields=_BAR_FIELDS, limit=_MAX_ROWS, **params),
                min(remaining, call_timeout + 1), "tushare daily batch",
            )
            if len(frame) >= _MAX_ROWS:
                raise ProviderError("Tushare batch reached row limit; completeness is unknown")
            if not frame.empty and not {"ts_code", "trade_date"}.issubset(frame.columns):
                raise ProviderError("Tushare batch missing security or date")
            return frame

        pending = list(codes)
        try:
            if (len(codes) + chunk_size - 1) // chunk_size > limit:
                days = []
                day = end
                while len(days) < limit:
                    if is_trading_day(day):
                        days.append(day.strftime("%Y%m%d"))
                    day -= timedelta(days=1)
                daily = []
                for day in days:
                    frame = query(trade_date=day)
                    if frame.empty or not (frame.trade_date == day).all():
                        raise ProviderError(f"Tushare missing or wrong daily cross-section {day}")
                    daily.append(frame[frame.ts_code.isin(codes)])
                joined = pd.concat(daily, ignore_index=True)
                frames = {code: frame for code, frame in joined.groupby("ts_code")}
                complete.update(code for code, frame in frames.items() if len(frame) >= limit)
                pending = [code for code in codes if code not in frames or len(frames[code]) < limit]
            for offset in range(0, len(pending), chunk_size):
                chunk = pending[offset:offset + chunk_size]
                frame = query(ts_code=",".join(chunk), start_date=start, end_date=end.strftime("%Y%m%d"))
                if not frame.empty and not frame.ts_code.isin(chunk).all():
                    raise ProviderError("Tushare batch returned unrequested securities")
                for code in chunk:
                    frames[code] = frame[frame.ts_code == code] if not frame.empty else frame
                    complete.add(code)
        except Exception as exc:
            # Only histories already downloaded completely may survive a failure.
            for code in codes:
                if code not in complete:
                    errors[codes[code]] = short_error_message(exc)

        for code, frame in frames.items():
            if code not in complete:
                continue
            symbol = codes[code]
            try:
                bars = _tushare_rows_to_bars(symbol, frame, limit)
                if bars[-1].timestamp.date() != end:
                    raise ProviderError("stale Tushare batch history")
                results[symbol] = bars
            except Exception as exc:
                errors[symbol] = short_error_message(exc)
        other_results, other_errors = self._get_individual_batch(others, limit=limit, timeout=deadline - time.monotonic())
        return {**results, **other_results}, {**errors, **other_errors}

    def _get_individual_batch(self, symbols: list[str], *, limit: int, timeout: float):
        """Serialize non-stock requests so the domain limiter cannot starve them."""
        deadline = time.monotonic() + timeout
        results, errors = {}, {}
        now = _market_now()
        end = _prev_trading_date(now.date(), now.hour)
        for symbol in symbols:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProviderError("Tushare batch time budget exhausted")
                bars = run_with_timeout(lambda symbol=symbol: self.get_bars(symbol, limit=limit), remaining, f"tushare batch {symbol}")
                if bars[-1].timestamp.date() != end:
                    raise ProviderError("stale Tushare batch history")
                results[symbol] = bars
            except Exception as exc:
                errors[symbol] = short_error_message(exc)
        return results, errors

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 120) -> list[Bar]:
        if timeframe in MINUTE_TIMEFRAMES:
            raise ProviderError(
                f"TushareProvider does not support minute timeframe {timeframe}; use akshare or sina"
            )
        if timeframe != "1d":
            raise ProviderError(f"unsupported timeframe: {timeframe}")

        if limit < 1 or limit > 6000:
            raise ProviderError("Tushare daily limit must be between 1 and 6000")
        # A request for N trading days needs more than N calendar days.
        start_date, _ = _date_window(limit * 2)
        now = _market_now()
        end_date = _prev_trading_date(now.date(), now.hour).strftime("%Y%m%d")
        ts_code = to_ts_code(symbol)

        def fetch():
            from trade_compass_agent.domain import InstrumentKind

            method = "daily"
            if is_index_symbol(symbol):
                method = "index_daily"
            elif is_lof_symbol(symbol) or infer_instrument_kind(symbol) == InstrumentKind.ETF:
                method = "fund_daily"
            return query_tushare(
                method, token=self._token, timeout=self.timeout,
                fields=_BAR_FIELDS,
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
            )

        try:
            df = run_with_timeout(fetch, self.timeout + 2, f"tushare {symbol}")
            return _tushare_rows_to_bars(symbol, df, limit)
        except Exception as exc:
            raise ProviderError(f"tushare failed for {symbol}: {short_error_message(exc)}") from exc
