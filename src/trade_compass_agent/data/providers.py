from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from trade_compass_agent.config import DataConfig

from trade_compass_agent.domain import Bar, Instrument, InstrumentKind

from .network import (
    extend_no_proxy_for_eastmoney,
    patch_requests_for_eastmoney,
    rate_limit_domain,
    run_with_timeout,
    short_error_message,
)


class MarketDataProvider(Protocol):
    name: str

    def get_instrument(self, symbol: str) -> Instrument: ...

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 120) -> list[Bar]: ...


class ProviderError(RuntimeError):
    pass


DEFAULT_REQUEST_TIMEOUT = 2.0
DEFAULT_BAOSTOCK_TIMEOUT = 4.0
MINUTE_TIMEFRAMES = {"1m", "5m", "15m", "30m", "60m"}
ALL_TIMEFRAMES = {"1d", *MINUTE_TIMEFRAMES}


def _is_trading_hours(now: datetime) -> bool:
    """True if *now* falls within A-share continuous trading window on a weekday."""
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return 930 <= t <= 1130 or 1300 <= t <= 1500


def _prev_trading_date(today: date, hour: int) -> date:
    from trade_compass_agent.ops.trading_calendar import is_trading_day

    d = today
    if hour < 15:
        d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def _market_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)


def split_symbol(symbol: str) -> tuple[str, str]:
    """Resolve a provider code without changing the caller's instrument identity."""
    value = symbol.strip().lower()
    match = re.fullmatch(r"(sh|sz|bj)\.?([0-9]{6})", value)
    if match:
        return match.group(1), match.group(2)
    match = re.fullmatch(r"([0-9]{6})\.(sh|sz|bj)", value)
    if match:
        return match.group(2), match.group(1)
    if not re.fullmatch(r"[0-9]{6}", value):
        raise ProviderError(f"invalid A-share symbol: {symbol}")
    market = "sh" if value.startswith(("5", "6", "9")) else "sz"
    if value.startswith(("4", "8")) or value.startswith("920"):
        market = "bj"
    return market, value


def is_index_symbol(symbol: str) -> bool:
    market, code = split_symbol(symbol)
    return (market == "sh" and code.startswith("000")) or (market == "sz" and code.startswith("399"))


def is_lof_symbol(symbol: str) -> bool:
    return split_symbol(symbol)[1].startswith(("16", "50"))


def infer_instrument_kind(symbol: str) -> InstrumentKind:
    try:
        s = split_symbol(symbol)[1]
    except ProviderError:
        s = symbol.strip()
    etf_prefixes = ("510", "511", "512", "513", "515", "516", "518", "159", "56", "588")
    if s.startswith(etf_prefixes):
        return InstrumentKind.ETF
    return InstrumentKind.STOCK


def to_baostock_code(symbol: str) -> str:
    market, code = split_symbol(symbol)
    return f"{market}.{code}"


def to_sina_code(symbol: str) -> str:
    market, code = split_symbol(symbol)
    return f"{market}{code}"


def _date_window(limit: int) -> tuple[str, str]:
    end = datetime.now()
    start = end - timedelta(days=max(limit + 40, 90))
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def _minute_window(limit: int, timeframe: str) -> tuple[str, str]:
    minutes = _timeframe_minutes(timeframe)
    end = datetime.now()
    # Leave enough calendar slack for lunch breaks, weekends, and non-trading hours.
    start = end - timedelta(minutes=max(limit * minutes * 3, 24 * 60))
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def _baostock_date_window(limit: int) -> tuple[str, str]:
    end = datetime.now()
    start = end - timedelta(days=max(limit + 40, 90))
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _timeframe_minutes(timeframe: str) -> int:
    mapping = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
    if timeframe not in mapping:
        raise ProviderError(f"unsupported timeframe: {timeframe}")
    return mapping[timeframe]


def _akshare_period(timeframe: str) -> str:
    if timeframe == "1d":
        raise ProviderError("daily timeframe has no minute period")
    return str(_timeframe_minutes(timeframe))


def _dataframe_to_bars(symbol: str, df, limit: int, *, adjusted: bool = True) -> list[Bar]:
    if df is None or getattr(df, "empty", True):
        raise ProviderError(f"no bars for {symbol}")

    df = df.tail(limit)
    bars: list[Bar] = []
    for _, row in df.iterrows():
        date_value = (
            row.get("日期")
            or row.get("时间")
            or row.get("day")
            or row.get("date")
            or row.get("time")
        )
        open_value = row.get("开盘") or row.get("open")
        high_value = row.get("最高") or row.get("high")
        low_value = row.get("最低") or row.get("low")
        close_value = row.get("收盘") or row.get("close")
        volume_value = row.get("成交量") or row.get("volume") or 0
        amount_value = row.get("成交额") or row.get("amount") or 0
        turnover_raw = row.get("换手率") or row.get("turnover")
        turnover_pct = float(turnover_raw) if turnover_raw is not None and str(turnover_raw).strip() not in ("", "nan", "None") else None
        timestamp = datetime.fromisoformat(str(date_value))
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=timestamp,
                open=float(open_value),
                high=float(high_value),
                low=float(low_value),
                close=float(close_value),
                volume=float(volume_value),
                amount=float(amount_value),
                adjusted=adjusted,
                turnover_pct=turnover_pct,
            )
        )
    if not bars:
        raise ProviderError(f"no bars for {symbol}")
    return bars


def _baostock_rows_to_bars(symbol: str, rows: list[list[str]], limit: int) -> list[Bar]:
    if not rows:
        raise ProviderError(f"baostock returned no bars for {symbol}")

    bars: list[Bar] = []
    for row in rows[-limit:]:
        date_value, open_value, high_value, low_value, close_value, volume_value, amount_value = row[:7]
        turnover_pct = float(row[7]) if len(row) > 7 and row[7] else None
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=datetime.fromisoformat(str(date_value)[:10]),
                open=float(open_value),
                high=float(high_value),
                low=float(low_value),
                close=float(close_value),
                volume=float(volume_value or 0),
                amount=float(amount_value or 0),
                adjusted=True,
                turnover_pct=turnover_pct,
            )
        )
    return bars


def create_market_data_provider(
    name: str = "auto",
    *,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    cache_dir: Path | None = None,
    data: DataConfig | None = None,
) -> MarketDataProvider:
    normalized = name.lower().strip()
    if normalized == "sample":
        return SampleProvider()
    if normalized == "tushare":
        from .tushare_provider import TushareProvider

        token_env = data.tushare_token_env if data else "TUSHARE_TOKEN"
        return TushareProvider(token_env=token_env, timeout=request_timeout)
    if normalized == "akshare":
        return AkshareProvider(timeout=request_timeout)
    if normalized == "sina":
        return SinaMinuteProvider(timeout=request_timeout)
    if normalized == "baostock":
        provider = ChainProvider(
            _available_providers(
                tushare=False,
                akshare=False,
                sina=False,
                baostock=True,
                cache_dir=cache_dir,
                data=data,
            ),
            timeout=request_timeout,
        )
        return provider
    if normalized == "auto":
        return ChainProvider(
            _available_providers(
                tushare=True,
                akshare=True,
                sina=True,
                baostock=True,
                cache_dir=cache_dir,
                data=data,
            ),
            timeout=request_timeout,
        )
    if normalized == "sina_daily":
        return SinaDailyProvider(timeout=request_timeout)
    if normalized == "tencent":
        from .tencent_provider import TencentProvider
        return TencentProvider(timeout=request_timeout)
    raise ValueError(f"Unknown data provider: {name}")


def create_bulk_daily_provider(
    *,
    cache_dir: Path | None = None,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    data: DataConfig | None = None,
) -> MarketDataProvider:
    """Cache-first daily bars optimized for parallel bulk screening."""
    return BulkDailyBarProvider(cache_dir=cache_dir, request_timeout=request_timeout, data=data)


def _maybe_tushare_provider(
    *,
    data: DataConfig | None,
    request_timeout: float,
) -> MarketDataProvider | None:
    if data is None or not data.tushare_enabled:
        return None
    token_env = data.tushare_token_env
    if not os.getenv(token_env, "").strip():
        return None
    try:
        from .tushare_provider import TushareProvider

        return TushareProvider(token_env=token_env, timeout=max(request_timeout, 3.0))
    except Exception:
        return None


def _with_preferred_provider(
    providers: list[MarketDataProvider], preferred: MarketDataProvider,
) -> list[MarketDataProvider]:
    """Reuse this source's cache first; other cached sources remain fallbacks."""
    ordered = [preferred, *providers]
    cache = next((p for p in providers if isinstance(p, LocalBarCacheProvider)), None)
    if cache is not None:
        preferred_cache = LocalBarCacheProvider(
            cache.root,
            closed_daily_only=cache.closed_daily_only or getattr(preferred, "closed_daily_only", False),
            required_source=preferred.name,
        )
        preferred_cache.supported_timeframes = getattr(preferred, "supported_timeframes", ALL_TIMEFRAMES)
        ordered.insert(0, preferred_cache)
    return ordered


def _available_providers(
    *,
    tushare: bool,
    akshare: bool,
    sina: bool,
    baostock: bool,
    cache_dir: Path | None,
    data: DataConfig | None = None,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
) -> list[MarketDataProvider]:
    providers: list[MarketDataProvider] = []
    # Free defaults; an explicitly enabled source takes precedence below.
    if cache_dir:
        providers.append(LocalBarCacheProvider(cache_dir))
    if sina:
        # Raw minute bars with turnover; daily requests skip this provider.
        providers.append(SinaMinuteProvider(timeout=request_timeout))
    if sina or akshare:
        from .tencent_provider import TencentProvider
        providers.append(TencentProvider(timeout=request_timeout))
    if sina:
        try:
            providers.append(SinaDailyProvider())
        except Exception:
            pass
    # BaostockProvider after Sina: provides turnover_pct but login can timeout (~4s)
    if baostock:
        try:
            providers.append(BaostockProvider())
        except Exception:
            pass
    if akshare:
        try:
            providers.append(AkshareProvider())
        except Exception:
            pass
    if tushare:
        tushare_provider = _maybe_tushare_provider(data=data, request_timeout=request_timeout)
        if tushare_provider is not None:
            providers = _with_preferred_provider(providers, tushare_provider)
    if not providers:
        raise RuntimeError(
            "No market data providers available. "
            "Install akshare/baostock/tushare or check network connectivity."
        )
    return providers


class SampleProvider:
    """Deterministic offline provider used for development and tests."""

    name = "sample"
    supported_timeframes = ALL_TIMEFRAMES

    def get_instrument(self, symbol: str) -> Instrument:
        kind = infer_instrument_kind(symbol)
        return Instrument(symbol=symbol, name=f"Sample {symbol}", kind=kind, exchange="SAMPLE")

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 120) -> list[Bar]:
        if timeframe == "1d":
            step = timedelta(days=1)
            now = datetime.now().replace(hour=15, minute=0, second=0, microsecond=0)
        else:
            step = timedelta(minutes=_timeframe_minutes(timeframe))
            now = _sample_intraday_now()
        base = 10 + (sum(ord(c) for c in symbol) % 50) / 10
        bars: list[Bar] = []
        for i in range(limit):
            drift = i * (0.015 if timeframe == "1d" else 0.003)
            wave = ((i % 7) - 3) * (0.03 if timeframe == "1d" else 0.01)
            close = base + drift + wave
            open_ = close * (1 - 0.004)
            high = close * 1.012
            low = close * 0.988
            volume = 1_000_000 + i * 8_000 + (i % 5) * 30_000
            bars.append(
                Bar(
                    symbol=symbol,
                    timestamp=now - step * (limit - 1 - i),
                    open=round(open_, 3),
                    high=round(high, 3),
                    low=round(low, 3),
                    close=round(close, 3),
                    volume=volume,
                    amount=volume * close,
                    adjusted=True,
                )
            )
        return bars


def _sample_intraday_now() -> datetime:
    now = datetime.now().replace(second=0, microsecond=0)
    morning_open = now.replace(hour=9, minute=30)
    market_close = now.replace(hour=15, minute=0)
    if morning_open <= now <= market_close:
        return now
    return market_close


class AkshareProvider:
    name = "akshare"
    supported_timeframes = ALL_TIMEFRAMES

    def __init__(self, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> None:
        extend_no_proxy_for_eastmoney()
        patch_requests_for_eastmoney(timeout)
        try:
            import akshare as ak  # type: ignore
        except Exception as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("akshare is not available; install akshare or check dependencies") from exc
        self.ak = ak
        self.timeout = timeout

    def get_instrument(self, symbol: str) -> Instrument:
        kind = infer_instrument_kind(symbol)
        return Instrument(symbol=symbol, name=symbol, kind=kind)

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 120) -> list[Bar]:
        if timeframe == "1d":
            start_date, end_date = _date_window(limit)
        else:
            start_date, end_date = _minute_window(limit, timeframe)

        def fetch():
            kind = infer_instrument_kind(symbol)
            code = split_symbol(symbol)[1]
            if is_index_symbol(symbol):
                if timeframe != "1d":
                    raise ProviderError("Akshare index minute bars require a supported fallback")
                return self.ak.index_zh_a_hist(symbol=code, period="daily", start_date=start_date, end_date=end_date)
            if timeframe != "1d":
                return self._minute_frame(symbol, timeframe, limit)
            if kind == InstrumentKind.ETF or is_lof_symbol(symbol):
                method = self.ak.fund_lof_hist_em if is_lof_symbol(symbol) else self.ak.fund_etf_hist_em
                return method(
                    symbol=code,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    adjust="qfq",
                )
            return self.ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="qfq",
                timeout=self.timeout,
            )

        try:
            df = run_with_timeout(fetch, self.timeout + 2, f"akshare {symbol}")
        except Exception as exc:
            raise ProviderError(f"akshare failed for {symbol}: {short_error_message(exc)}") from exc

        return _dataframe_to_bars(symbol, df, limit, adjusted=timeframe == "1d")

    def _minute_frame(self, symbol: str, timeframe: str, limit: int):
        """Request one instrument, without the SDK's full fund-code lookup/retries."""
        import pandas as pd
        import requests

        if timeframe not in MINUTE_TIMEFRAMES:
            raise ProviderError(f"unsupported timeframe: {timeframe}")
        market, code = split_symbol(symbol)
        params = {
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "secid": f"{1 if market == 'sh' else 0}.{code}",
        }
        columns = ["时间", "开盘", "收盘", "最高", "最低", "成交量", "成交额"]
        if timeframe == "1m":
            url = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
            params.update(fields2="f51,f52,f53,f54,f55,f56,f57,f58", ndays="5", iscr="0")
            key = "trends"
            columns += ["均价"]
        else:
            url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
            params.update(fields2="f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                          klt=_akshare_period(timeframe), fqt="0", beg="0", end="20500000", lmt=str(limit))
            key = "klines"
            columns += ["振幅", "涨跌幅", "涨跌额", "换手率"]
        rate_limit_domain(url)
        response = requests.get(url, params=params,
                                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
                                timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        rows = (payload.get("data") or {}).get(key) or []
        if not rows:
            raise ProviderError(f"empty Eastmoney {key} response (rc={payload.get('rc')})")
        return pd.DataFrame([row.split(",") for row in rows], columns=columns)


class SinaMinuteProvider:
    name = "sina"
    supported_timeframes = MINUTE_TIMEFRAMES

    def __init__(self, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> None:
        self.timeout = timeout

    def get_instrument(self, symbol: str) -> Instrument:
        kind = infer_instrument_kind(symbol)
        return Instrument(symbol=symbol, name=symbol, kind=kind)

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 120) -> list[Bar]:
        if timeframe not in MINUTE_TIMEFRAMES:
            raise ProviderError("SinaMinuteProvider only supports minute bars")

        def fetch():
            import pandas as pd
            import requests

            # The public endpoint also serves exchange-traded funds and indices.
            # The SDK fetches 1970 rows and probes daily adjustment data even when
            # adjust=""; neither extra request is needed for raw minute bars.
            response = requests.get(
                "https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData",
                params={"symbol": to_sina_code(symbol), "scale": _akshare_period(timeframe),
                        "ma": "no", "datalen": str(max(1, min(limit, 1970)))},
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            text = response.text
            if "=(" not in text or ");" not in text:
                raise ProviderError("invalid Sina minute JSONP response")
            data = json.loads(text.split("=(", 1)[1].rsplit(");", 1)[0])
            return pd.DataFrame(data)

        try:
            df = run_with_timeout(fetch, self.timeout + 2, f"sina {symbol}")
            return _dataframe_to_bars(symbol, df, limit, adjusted=False)
        except Exception as exc:
            raise ProviderError(f"sina failed for {symbol}: {short_error_message(exc)}") from exc


class SinaDailyProvider:
    """Direct HTTP provider for daily K-lines from Sina Finance. Works 24/7, no akshare."""

    name = "sina_daily"
    supported_timeframes = {"1d"}
    _cooldown_until = 0.0
    _cooldown_status = 0
    _cooldown_lock = threading.Lock()

    def __init__(self, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> None:
        self.timeout = timeout

    def get_instrument(self, symbol: str) -> Instrument:
        kind = infer_instrument_kind(symbol)
        return Instrument(symbol=symbol, name=symbol, kind=kind)

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 120) -> list[Bar]:
        if timeframe != "1d":
            raise ProviderError("SinaDailyProvider only supports daily bars")
        with self._cooldown_lock:
            remaining = type(self)._cooldown_until - time.monotonic()
            if remaining > 0:
                raise ProviderError(f"Sina daily HTTP {type(self)._cooldown_status}; retry after {remaining:.0f}s")

        url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
        params = {"symbol": to_sina_code(symbol), "scale": "240", "ma": "no", "datalen": str(min(limit + 10, 300))}
        headers = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}

        import requests
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=self.timeout)
            if resp.status_code in {429, 456}:
                with self._cooldown_lock:
                    type(self)._cooldown_until = time.monotonic() + 60
                    type(self)._cooldown_status = resp.status_code
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise ProviderError(f"Sina daily bars failed for {symbol}: {short_error_message(exc)}") from exc

        if not data:
            raise ProviderError(f"Sina daily returned no data for {symbol}")

        from datetime import datetime
        bars = []
        for item in data[-limit:]:
            bars.append(Bar(
                symbol=symbol,
                timestamp=datetime.strptime(item["day"], "%Y-%m-%d"),
                open=float(item["open"]),
                high=float(item["high"]),
                low=float(item["low"]),
                close=float(item["close"]),
                volume=int(float(item.get("volume", 0))),
            ))
        return bars


_BAOSTOCK_SLOTS = threading.BoundedSemaphore(4)


class BaostockProvider:
    name = "baostock"
    supported_timeframes = {"1d"}

    def __init__(self, timeout: float = DEFAULT_BAOSTOCK_TIMEOUT) -> None:
        from importlib.util import find_spec
        if find_spec("baostock") is None:
            raise RuntimeError("baostock is not available")
        self.timeout = timeout

    def get_instrument(self, symbol: str) -> Instrument:
        kind = infer_instrument_kind(symbol)
        return Instrument(symbol=symbol, name=symbol, kind=kind)

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 120) -> list[Bar]:
        if timeframe != "1d":
            raise ProviderError("BaostockProvider only supports daily bars in this MVP")
        import subprocess
        import sys

        start_date, end_date = _baostock_date_window(limit)
        request = {
            "symbol": to_baostock_code(symbol), "start_date": start_date,
            "end_date": end_date, "timeout": self.timeout,
        }
        deadline = time.monotonic() + self.timeout
        if not _BAOSTOCK_SLOTS.acquire(timeout=self.timeout):
            raise ProviderError(f"baostock {symbol} timed out waiting for an available connection")
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError(f"baostock {symbol} timed out waiting for an available connection")
            request["timeout"] = remaining
            result = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("_baostock_worker.py"))],
                input=json.dumps(request), capture_output=True, text=True,
                timeout=remaining, check=True,
            )
            response = json.loads(result.stdout)
            if response.get("error"):
                raise ProviderError(response["error"])
            return _baostock_rows_to_bars(symbol, response["rows"], limit)
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"baostock {symbol} timed out after {self.timeout:.1f}s") from exc
        except Exception as exc:
            raise ProviderError(f"baostock failed for {symbol}: {short_error_message(exc)}") from exc
        finally:
            _BAOSTOCK_SLOTS.release()


def _bars_are_fresh(bars: list[Bar], timeframe: str = "1d", path: Path | None = None) -> bool:
    from trade_compass_agent.ops.trading_calendar import is_trading_day

    now = _market_now()
    latest = bars[-1].timestamp.date()
    today = now.date()
    opened = is_trading_day(today) and now.hour * 100 + now.minute >= 930
    min_date = today if opened else _prev_trading_date(today, now.hour)
    if latest < min_date:
        return False
    session_close = datetime.combine(min_date, datetime.min.time()).replace(hour=15)
    if timeframe in MINUTE_TIMEFRAMES:
        endpoint = min(now, session_close) if opened else session_close
        if opened and 1130 <= now.hour * 100 + now.minute < 1300:
            endpoint = now.replace(hour=11, minute=30, second=0, microsecond=0)
        return bars[-1].timestamp >= endpoint - timedelta(minutes=_timeframe_minutes(timeframe), seconds=60)
    if timeframe == "1d" and path is not None:
        written = datetime.fromtimestamp(path.stat().st_mtime, ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        if not opened or now.hour >= 15:
            return written >= session_close
        return now - written <= timedelta(minutes=1)
    return True


class LocalBarCacheProvider:
    name = "cache"
    _write_lock = threading.Lock()
    supported_timeframes = ALL_TIMEFRAMES

    def __init__(self, root: Path, *, closed_daily_only: bool = False, required_source: str | None = None) -> None:
        self.root = root
        self.closed_daily_only = closed_daily_only
        self.required_source = required_source
        self.root.mkdir(parents=True, exist_ok=True)

    def get_instrument(self, symbol: str) -> Instrument:
        kind = infer_instrument_kind(symbol)
        return Instrument(symbol=symbol, name=symbol, kind=kind, exchange="CACHE")

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 120) -> list[Bar]:
        path = self._path(symbol, timeframe)
        if not path.exists():
            raise ProviderError(f"no cache for {symbol} {timeframe}")
        bars: list[Bar] = []
        requested_limit = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProviderError(f"invalid bar cache for {symbol} {timeframe}") from exc
            requested_limit = max(requested_limit, int(raw.get("requested_limit", 0)))
            bars.append(
                Bar(
                    symbol=str(raw["symbol"]),
                    timestamp=datetime.fromisoformat(str(raw["timestamp"])),
                    open=float(raw["open"]),
                    high=float(raw["high"]),
                    low=float(raw["low"]),
                    close=float(raw["close"]),
                    volume=float(raw["volume"]),
                    amount=float(raw["amount"]) if raw.get("amount") is not None else None,
                    adjusted=bool(raw.get("adjusted", False)),
                    turnover_pct=raw.get("turnover_pct"),
                    source=raw.get("source"),
                )
            )
        if timeframe == "1d" and self.closed_daily_only:
            now = _market_now()
            last_closed = _prev_trading_date(now.date(), now.hour)
            bars = [bar for bar in bars if bar.timestamp.date() <= last_closed]
        if not bars:
            raise ProviderError(f"empty cache for {symbol} {timeframe}")
        if self.required_source is not None and any(bar.source != self.required_source for bar in bars):
            raise ProviderError(f"cache is not from preferred source {self.required_source}")
        if not self._is_fresh(bars, timeframe, path):
            raise ProviderError(f"stale cache for {symbol} {timeframe}")
        if len(bars) < limit and requested_limit < limit:
            raise ProviderError(
                f"cache has {len(bars)} bars for {symbol} {timeframe}, need {limit}"
            )
        return bars[-limit:]

    def _is_fresh(self, bars: list[Bar], timeframe: str = "1d", path: Path | None = None) -> bool:
        if timeframe == "1d" and self.closed_daily_only:
            now = _market_now()
            last_closed = _prev_trading_date(now.date(), now.hour)
            if path is not None:
                written = datetime.fromtimestamp(path.stat().st_mtime, ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
                if written < datetime.combine(last_closed, datetime.min.time()).replace(hour=15):
                    return False
            return bool(bars) and bars[-1].timestamp.date() >= last_closed
        return _bars_are_fresh(bars, timeframe, path)

    def write_bars(self, symbol: str, timeframe: str, bars: list[Bar], *, requested_limit: int = 0) -> None:
        if not bars:
            return
        with self._write_lock:
            path = self._path(symbol, timeframe)
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = {}
            if path.exists():
                try:
                    for line in path.read_text(encoding="utf-8").splitlines():
                        if line.strip():
                            row = json.loads(line)
                            key = row["timestamp"][:10] if timeframe == "1d" else row["timestamp"]
                            existing[key] = row
                except (json.JSONDecodeError, KeyError, TypeError):
                    import shutil
                    from uuid import uuid4
                    shutil.copy2(path, path.with_suffix(f".corrupt-{uuid4().hex}"))
                    existing.clear()
            if any(bool(row.get("adjusted", False)) != bars[0].adjusted or row.get("source") != bars[0].source for row in existing.values()):
                existing.clear()
            # A changed adjustment basis invalidates older, non-overlapping prices.
            if timeframe == "1d" and bars[0].adjusted:
                for bar in bars:
                    previous = existing.get(bar.timestamp.date().isoformat())
                    if previous and float(previous["close"]) != bar.close:
                        existing.clear()
                        break
            for bar in bars:
                key = bar.timestamp.date().isoformat() if timeframe == "1d" else bar.timestamp.isoformat()
                existing[key] = {
                    "symbol": bar.symbol, "timestamp": bar.timestamp.isoformat(),
                    "open": bar.open, "high": bar.high, "low": bar.low,
                    "close": bar.close, "volume": bar.volume, "amount": bar.amount,
                    "adjusted": bar.adjusted,
                    "turnover_pct": bar.turnover_pct, "source": bar.source,
                    "requested_limit": requested_limit,
                }
            rows = [existing[key] for key in sorted(existing)[-1000:]]
            # Readers must see either complete version, including while batch fetches run.
            import tempfile

            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as handle:
                temporary = Path(handle.name)
                try:
                    for row in rows:
                        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                    handle.flush()
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)

    def _path(self, symbol: str, timeframe: str) -> Path:
        return self.root / timeframe / f"{symbol}.jsonl"


class BulkDailyBarProvider:
    """Daily screening bars using the same source preference as normal queries.

    Cache freshness: only use cached bars if the latest bar date >= the
    expected latest trading date (previous trading day). This prevents
    morning_plan or other jobs from analysing stale data.
    """

    name = "bulk_daily"
    supported_timeframes = {"1d"}

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        data: DataConfig | None = None,
    ) -> None:
        from .tencent_provider import TencentProvider
        network: list[MarketDataProvider] = [TencentProvider(timeout=request_timeout), SinaDailyProvider(timeout=request_timeout)]
        try:
            network.append(BaostockProvider(timeout=max(request_timeout, DEFAULT_BAOSTOCK_TIMEOUT)))
        except Exception:
            pass
        self._cache = LocalBarCacheProvider(cache_dir, closed_daily_only=True) if cache_dir else None
        if self._cache is not None:
            network.insert(0, self._cache)
        optional = _maybe_tushare_provider(data=data, request_timeout=request_timeout)
        if optional is not None:
            network = _with_preferred_provider(network, optional)
            if self._cache is not None:
                self._cache = network[0]
        self._network = ChainProvider(network, timeout=request_timeout)

    def get_instrument(self, symbol: str) -> Instrument:
        return self._network.get_instrument(symbol)

    def prefetch_bars(self, symbols: list[str], *, timeframe: str = "1d", limit: int = 120, timeout: float = 20) -> None:
        # The outer consumer still applies the same closed-day and fallback rules.
        requested = limit + int(_market_now().date() > self._get_min_date())
        self._network.prefetch_bars(symbols, timeframe=timeframe, limit=requested, timeout=timeout)

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 120) -> list[Bar]:
        if timeframe != "1d":
            raise ProviderError("BulkDailyBarProvider only supports daily bars")
        if self._cache is not None:
            try:
                bars = self._cache.get_bars(symbol, timeframe=timeframe, limit=limit)
                if self._cache_is_fresh(bars):
                    return bars
            except ProviderError:
                pass
        last_closed = self._get_min_date()
        requested = limit + int(_market_now().date() > last_closed)
        bars = self._network.get_bars(symbol, timeframe=timeframe, limit=requested)
        return [bar for bar in bars if bar.timestamp.date() <= last_closed][-limit:]

    def _cache_is_fresh(self, bars: list[Bar]) -> bool:
        """Check if cached bars include data up to at least the previous trading day."""
        if not bars:
            return False
        min_date = self._get_min_date()
        return bars[-1].timestamp.date() >= min_date

    def _get_min_date(self) -> date:
        """Recompute on every call; this provider lives across sessions and days."""
        now = _market_now()
        return _prev_trading_date(now.date(), now.hour)


class ChainProvider:
    """Try real providers in order; only use sample when all real sources fail."""

    name = "auto"

    def __init__(
        self,
        providers: list[MarketDataProvider],
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        *,
        total_timeout: float | None = None,
    ) -> None:
        if not providers:
            raise ValueError("ChainProvider requires at least one provider")
        self.providers = providers
        self.timeout = timeout
        self.total_timeout = total_timeout if total_timeout is not None else timeout * 4
        self.last_warnings: list[str] = []
        self.last_resolved_provider: str | None = None
        self._prefetch_failures: dict[tuple[str, str, int], str] = {}

    def prefetch_bars(self, symbols: list[str], *, timeframe: str = "1d", limit: int = 120, timeout: float = 20) -> None:
        """Fill the existing preferred-source cache before a batch consumer reads it."""
        if timeframe != "1d":
            return
        preferred = next((p for p in self.providers if p.name not in {"cache", "sample"}
                          and self._supports_timeframe(p, timeframe)), None)
        fetch = getattr(preferred, "get_bars_batch", None)
        cache = next((p for p in self.providers if isinstance(p, LocalBarCacheProvider)
                      and p.required_source == getattr(preferred, "name", None)), None)
        if not callable(fetch) or cache is None:
            return
        pending = []
        for symbol in dict.fromkeys(symbols):
            self._prefetch_failures.pop((symbol, timeframe, limit), None)
            try:
                cache.get_bars(symbol, timeframe=timeframe, limit=limit)
            except ProviderError:
                pending.append(symbol)
        if not pending:
            return
        try:
            results, errors = fetch(pending, limit=limit, timeout=timeout)
        except Exception as exc:
            results, errors = {}, {symbol: short_error_message(exc) for symbol in pending}
        for symbol, bars in results.items():
            cache.write_bars(symbol, timeframe, bars, requested_limit=limit)
        self._prefetch_failures.update({(symbol, timeframe, limit): error for symbol, error in errors.items()})

    def get_instrument(self, symbol: str) -> Instrument:
        for provider in self.providers:
            if provider.name == "sample":
                continue
            try:
                return provider.get_instrument(symbol)
            except Exception:
                continue
        raise ProviderError(f"get_instrument failed for {symbol}: no provider returned data")

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 120) -> list[Bar]:
        real_providers = [
            provider
            for provider in self.providers
            if provider.name != "sample" and self._supports_timeframe(provider, timeframe)
        ]
        cache_provider = next(
            (provider for provider in self.providers if isinstance(provider, LocalBarCacheProvider)),
            None,
        )

        deadline = time.monotonic() + self.total_timeout
        budget_exhausted = False
        failures: list[str] = []
        prefetch_error = self._prefetch_failures.pop((symbol, timeframe, limit), None)
        for provider in real_providers:
            if prefetch_error and callable(getattr(provider, "get_bars_batch", None)):
                failures.append(f"{provider.name}: {prefetch_error}")
                self._record_failure(symbol, provider, ProviderError(prefetch_error))
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                budget_exhausted = True
                break
            try:
                if isinstance(provider, LocalBarCacheProvider):
                    bars = provider.get_bars(symbol, timeframe=timeframe, limit=limit)
                else:
                    call_timeout = min(remaining, max(self.timeout, getattr(provider, "timeout", self.timeout)) + 1)
                    bars = run_with_timeout(
                        lambda provider=provider: provider.get_bars(symbol, timeframe=timeframe, limit=limit),
                        call_timeout, f"{provider.name} {symbol}",
                    )
                if time.monotonic() >= deadline:
                    raise ProviderError("data provider timeout budget exhausted")
                if not bars:
                    raise ProviderError("empty bars response")
                if timeframe == "1d":
                    now = _market_now()
                    expected = _prev_trading_date(now.date(), now.hour)
                    if bars[-1].timestamp.date() < expected:
                        raise ProviderError(f"stale daily bars: latest={bars[-1].timestamp.date()}, required={expected}")
                if timeframe in MINUTE_TIMEFRAMES and not _bars_are_fresh(bars, timeframe):
                    raise ProviderError(f"stale minute bars: latest={bars[-1].timestamp}")
                self.last_resolved_provider = provider.name
                if provider.name != "cache":
                    bars = [replace(bar, source=provider.name) if bar.source is None else bar for bar in bars]
                if provider.name not in {"cache", "sample"} and cache_provider is not None:
                    cache_provider.write_bars(symbol, timeframe, bars, requested_limit=limit)
                return bars
            except Exception as exc:
                failures.append(f"{provider.name}: {short_error_message(exc)}")
                self._record_failure(symbol, provider, exc)
                if time.monotonic() >= deadline:
                    budget_exhausted = True
                    break

        if budget_exhausted:
            raise ProviderError(
                f"{symbol}: data provider timeout budget exhausted after "
                f"{self.total_timeout:.1f}s for timeframe={timeframe}. {'; '.join(failures)}"
            )

        raise ProviderError(
            f"{symbol}: all data providers failed for timeframe={timeframe}. "
            f"Tried: {[p.name for p in real_providers]}. "
            f"{' ; '.join(failures)}"
        )

    def _record_failure(self, symbol: str, provider: MarketDataProvider, exc: Exception) -> None:
        if provider.name == "akshare":
            message = (
                f"{symbol}: 东方财富行情源暂不可用（{short_error_message(exc)}），"
                "正在尝试备用数据源…"
            )
        elif provider.name == "baostock":
            message = f"{symbol}: Baostock 不可用（{short_error_message(exc)}）。"
        elif provider.name == "tushare":
            message = f"{symbol}: Tushare 不可用（{short_error_message(exc)}），正在尝试备用数据源…"
        else:
            message = f"{symbol}: {provider.name} 不可用（{short_error_message(exc)}）。"
        self.last_warnings.append(message)

    def _supports_timeframe(self, provider: MarketDataProvider, timeframe: str) -> bool:
        supported = getattr(provider, "supported_timeframes", None)
        return supported is None or timeframe in supported


# Backward-compatible alias used by older imports/tests.
class FallbackProvider(ChainProvider):
    pass
