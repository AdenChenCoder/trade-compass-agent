from __future__ import annotations

import os
from datetime import datetime

from trade_compass_agent.domain import Bar, Instrument

from .network import run_with_timeout, short_error_message
from .providers import (
    DEFAULT_REQUEST_TIMEOUT,
    MINUTE_TIMEFRAMES,
    ProviderError,
    _date_window,
    infer_instrument_kind,
)


def to_ts_code(symbol: str) -> str:
    """Map A-share symbol to Tushare ts_code (e.g. 600519 -> 600519.SH)."""

    normalized = symbol.strip()
    if normalized.startswith(("5", "6", "9")):
        return f"{normalized}.SH"
    return f"{normalized}.SZ"


def _tushare_rows_to_bars(symbol: str, df, limit: int) -> list[Bar]:
    if df is None or getattr(df, "empty", True):
        raise ProviderError(f"no bars for {symbol}")

    df = df.sort_values("trade_date").tail(limit)
    bars: list[Bar] = []
    for _, row in df.iterrows():
        date_value = row.get("trade_date") or row.get("date")
        timestamp = datetime.strptime(str(date_value)[:8], "%Y%m%d")
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=timestamp.replace(hour=15, minute=0, second=0, microsecond=0),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("vol", row.get("volume", 0)) or 0),
                amount=float(row.get("amount", 0) or 0),
                adjusted=True,
            )
        )
    if not bars:
        raise ProviderError(f"no bars for {symbol}")
    return bars


class TushareProvider:
    """Optional Tushare Pro daily bars (token-gated)."""

    name = "tushare"
    supported_timeframes = {"1d"}

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
                f"Tushare token missing; set env {token_env} or install optional extra: pip install -e '.[tushare]'"
            )
        self.timeout = timeout
        self._pro = None

    def _api(self):
        if self._pro is not None:
            return self._pro
        try:
            import tushare as ts  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ProviderError(
                "tushare package not installed; install with: pip install -e '.[tushare]'"
            ) from exc
        ts.set_token(self._token)
        self._pro = ts.pro_api(self._token)
        return self._pro

    def get_instrument(self, symbol: str) -> Instrument:
        kind = infer_instrument_kind(symbol)
        return Instrument(symbol=symbol, name=symbol, kind=kind, exchange="TUSHARE")

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 120) -> list[Bar]:
        if timeframe in MINUTE_TIMEFRAMES:
            raise ProviderError(
                f"TushareProvider does not support minute timeframe {timeframe}; use akshare or sina"
            )
        if timeframe != "1d":
            raise ProviderError(f"unsupported timeframe: {timeframe}")

        start_date, end_date = _date_window(limit)
        ts_code = to_ts_code(symbol)

        def fetch():
            return self._api().daily(
                ts_code=ts_code,
                start_date=start_date,
                end_date=end_date,
            )

        try:
            df = run_with_timeout(fetch, self.timeout + 2, f"tushare {symbol}")
        except Exception as exc:
            raise ProviderError(f"tushare failed for {symbol}: {short_error_message(exc)}") from exc

        return _tushare_rows_to_bars(symbol, df, limit)
