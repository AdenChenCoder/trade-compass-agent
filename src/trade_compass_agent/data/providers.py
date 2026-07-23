from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from trade_compass_agent.config import DataConfig

from trade_compass_agent.domain import Bar, Instrument, InstrumentKind

from .network import (
    extend_no_proxy_for_eastmoney,
    patch_requests_for_eastmoney,
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
    return 930 <= t <= 1500


def _prev_trading_date(today: date, hour: int) -> date:
    d = today
    if hour < 16:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def infer_instrument_kind(symbol: str) -> InstrumentKind:
    s = symbol.strip()
    etf_prefixes = ("510", "511", "512", "513", "515", "516", "518", "159", "588")
    if s.startswith(etf_prefixes):
        return InstrumentKind.ETF
    return InstrumentKind.STOCK


def to_baostock_code(symbol: str) -> str:
    normalized = symbol.strip()
    if normalized.startswith(("5", "6", "9")):
        return f"sh.{normalized}"
    return f"sz.{normalized}"


def to_sina_code(symbol: str) -> str:
    normalized = symbol.strip()
    prefix = "sh" if normalized.startswith(("5", "6", "9")) else "sz"
    return f"{prefix}{normalized}"


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


def _dataframe_to_bars(symbol: str, df, limit: int) -> list[Bar]:
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
                adjusted=True,
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
    raise ValueError(f"Unknown data provider: {name}")


def create_bulk_daily_provider(
    *,
    cache_dir: Path | None = None,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
) -> MarketDataProvider:
    """Cache-first daily bars optimized for parallel bulk screening."""
    return BulkDailyBarProvider(cache_dir=cache_dir, request_timeout=request_timeout)


def _maybe_tushare_provider(
    *,
    data: DataConfig | None,
    request_timeout: float,
    allow_without_flag: bool = False,
) -> MarketDataProvider | None:
    token_env = data.tushare_token_env if data else "TUSHARE_TOKEN"
    if data is not None and not data.tushare_enabled and not allow_without_flag:
        return None
    if not os.getenv(token_env, "").strip():
        return None
    try:
        from .tushare_provider import TushareProvider

        return TushareProvider(token_env=token_env, timeout=request_timeout)
    except Exception:
        return None


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
    # Cache first: instant return when fresh bars are already available
    if cache_dir:
        providers.append(LocalBarCacheProvider(cache_dir))
    if tushare:
        tushare_provider = _maybe_tushare_provider(data=data, request_timeout=request_timeout)
        if tushare_provider is not None:
            providers.append(tushare_provider)
    # SinaDailyProvider preferred: direct HTTP, 24/7 stable, fast (~0.5s)
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
    if sina:
        try:
            providers.append(SinaMinuteProvider())
        except Exception:
            pass
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
            if timeframe != "1d":
                method = (
                    self.ak.fund_etf_hist_min_em
                    if kind == InstrumentKind.ETF
                    else self.ak.stock_zh_a_hist_min_em
                )
                return method(
                    symbol=symbol,
                    start_date=start_date,
                    end_date=end_date,
                    period=_akshare_period(timeframe),
                    adjust="",
                )
            if kind == InstrumentKind.ETF:
                return self.ak.fund_etf_hist_em(
                    symbol=symbol,
                    period="daily",
                    start_date=start_date,
                    end_date=end_date,
                    adjust="qfq",
                )
            return self.ak.stock_zh_a_hist(
                symbol=symbol,
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

        return _dataframe_to_bars(symbol, df, limit)


class SinaMinuteProvider:
    name = "sina"
    supported_timeframes = MINUTE_TIMEFRAMES

    def __init__(self, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> None:
        try:
            import akshare as ak  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("akshare is not available; SinaMinuteProvider uses akshare wrappers") from exc
        self.ak = ak
        self.timeout = timeout

    def get_instrument(self, symbol: str) -> Instrument:
        kind = infer_instrument_kind(symbol)
        return Instrument(symbol=symbol, name=symbol, kind=kind)

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 120) -> list[Bar]:
        if timeframe not in MINUTE_TIMEFRAMES:
            raise ProviderError("SinaMinuteProvider only supports minute bars")
        if infer_instrument_kind(symbol) != InstrumentKind.STOCK:
            raise ProviderError("SinaMinuteProvider currently supports A-share stocks only")

        def fetch():
            return self.ak.stock_zh_a_minute(
                symbol=to_sina_code(symbol),
                period=_akshare_period(timeframe),
                adjust="",
            )

        try:
            df = run_with_timeout(fetch, self.timeout + 2, f"sina {symbol}")
        except Exception as exc:
            raise ProviderError(f"sina failed for {symbol}: {short_error_message(exc)}") from exc
        return _dataframe_to_bars(symbol, df, limit)


class SinaDailyProvider:
    """Direct HTTP provider for daily K-lines from Sina Finance. Works 24/7, no akshare."""

    name = "sina_daily"
    supported_timeframes = {"1d"}

    def __init__(self, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> None:
        self.timeout = timeout

    def get_instrument(self, symbol: str) -> Instrument:
        kind = infer_instrument_kind(symbol)
        return Instrument(symbol=symbol, name=symbol, kind=kind)

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 120) -> list[Bar]:
        if timeframe != "1d":
            raise ProviderError("SinaDailyProvider only supports daily bars")

        prefix = "sz" if symbol.startswith(("0", "3")) else "sh"
        url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
        params = {"symbol": f"{prefix}{symbol}", "scale": "240", "ma": "no", "datalen": str(min(limit + 10, 300))}
        headers = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}

        import requests
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=self.timeout)
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


class _BaostockSession:
    _lock = threading.Lock()
    _active = False

    @classmethod
    def ensure_login(cls, bs_module) -> None:
        with cls._lock:
            if cls._active:
                return
            import io, contextlib
            with contextlib.redirect_stdout(io.StringIO()):
                login_result = bs_module.login()
            if login_result.error_code != "0":
                raise ProviderError(f"baostock login failed: {login_result.error_msg}")
            cls._active = True

    @classmethod
    def reset(cls, bs_module) -> None:
        with cls._lock:
            if cls._active:
                import io, contextlib
                with contextlib.redirect_stdout(io.StringIO()):
                    bs_module.logout()
            cls._active = False


class BaostockProvider:
    name = "baostock"
    supported_timeframes = {"1d"}

    def __init__(self, timeout: float = DEFAULT_BAOSTOCK_TIMEOUT) -> None:
        try:
            import baostock as bs  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("baostock is not available") from exc
        self.bs = bs
        self.timeout = timeout

    def get_instrument(self, symbol: str) -> Instrument:
        kind = infer_instrument_kind(symbol)
        return Instrument(symbol=symbol, name=symbol, kind=kind)

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 120) -> list[Bar]:
        if timeframe != "1d":
            raise ProviderError("BaostockProvider only supports daily bars in this MVP")

        start_date, end_date = _baostock_date_window(limit)
        bs_code = to_baostock_code(symbol)
        fields = "date,open,high,low,close,volume,amount,turn"

        def fetch_rows() -> list[list[str]]:
            import io, contextlib
            with contextlib.redirect_stdout(io.StringIO()):
                _BaostockSession.ensure_login(self.bs)
                result = self.bs.query_history_k_data_plus(
                    bs_code,
                    fields,
                    start_date=start_date,
                    end_date=end_date,
                    frequency="d",
                    adjustflag="2",
                )
            if result.error_code != "0":
                raise ProviderError(f"baostock query failed for {symbol}: {result.error_msg}")
            rows: list[list[str]] = []
            with contextlib.redirect_stdout(io.StringIO()):
                while result.error_code == "0" and result.next():
                    rows.append(result.get_row_data())
            return rows

        try:
            rows = run_with_timeout(fetch_rows, self.timeout, f"baostock {symbol}")
        except Exception as exc:
            err_msg = str(exc)
            if "decompressing" in err_msg or "接收数据异常" in err_msg:
                _BaostockSession.reset(self.bs)
                try:
                    rows = run_with_timeout(fetch_rows, self.timeout, f"baostock {symbol} retry")
                except Exception as retry_exc:
                    _BaostockSession.reset(self.bs)
                    raise ProviderError(f"baostock failed for {symbol}: {short_error_message(retry_exc)}") from retry_exc
            else:
                _BaostockSession.reset(self.bs)
                raise ProviderError(f"baostock failed for {symbol}: {short_error_message(exc)}") from exc

        return _baostock_rows_to_bars(symbol, rows, limit)


class LocalBarCacheProvider:
    name = "cache"
    supported_timeframes = ALL_TIMEFRAMES

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def get_instrument(self, symbol: str) -> Instrument:
        kind = infer_instrument_kind(symbol)
        return Instrument(symbol=symbol, name=symbol, kind=kind, exchange="CACHE")

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 120) -> list[Bar]:
        path = self._path(symbol, timeframe)
        if not path.exists():
            raise ProviderError(f"no cache for {symbol} {timeframe}")
        bars: list[Bar] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
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
                )
            )
        if not bars:
            raise ProviderError(f"empty cache for {symbol} {timeframe}")
        if timeframe == "1d" and not self._is_fresh(bars):
            raise ProviderError(f"stale cache for {symbol} {timeframe}")
        if len(bars) < limit:
            raise ProviderError(
                f"cache has {len(bars)} bars for {symbol} {timeframe}, need {limit}"
            )
        return bars[-limit:]

    def _is_fresh(self, bars: list[Bar]) -> bool:
        now = datetime.now()
        latest = bars[-1].timestamp.date()
        today = now.date()
        if _is_trading_hours(now):
            # During trading hours, cache must include today's partial bar
            return latest >= today
        # Outside trading hours, accept previous trading day
        min_date = _prev_trading_date(today, now.hour)
        return latest >= min_date

    def write_bars(self, symbol: str, timeframe: str, bars: list[Bar]) -> None:
        if not bars:
            return
        path = self._path(symbol, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing_count = sum(1 for _ in path.read_text(encoding="utf-8").splitlines() if _.strip()) if path.exists() else 0
        if len(bars) < existing_count:
            return
        with path.open("w", encoding="utf-8") as handle:
            for bar in bars[-1000:]:
                handle.write(
                    json.dumps(
                        {
                            "symbol": bar.symbol,
                            "timestamp": bar.timestamp.isoformat(),
                            "open": bar.open,
                            "high": bar.high,
                            "low": bar.low,
                            "close": bar.close,
                            "volume": bar.volume,
                            "amount": bar.amount,
                            "adjusted": bar.adjusted,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    def _path(self, symbol: str, timeframe: str) -> Path:
        return self.root / timeframe / f"{symbol}.jsonl"


class BulkDailyBarProvider:
    """Cache-first daily bars for bulk screening — Sina HTTP primary, baostock fallback.

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
    ) -> None:
        network: list[MarketDataProvider] = [SinaDailyProvider(timeout=request_timeout)]
        try:
            network.append(BaostockProvider(timeout=max(request_timeout, DEFAULT_BAOSTOCK_TIMEOUT)))
        except Exception:
            pass
        self._cache = LocalBarCacheProvider(cache_dir) if cache_dir else None
        self._network = ChainProvider(network, timeout=request_timeout)
        self._min_date: datetime | None = None

    def get_instrument(self, symbol: str) -> Instrument:
        return self._network.get_instrument(symbol)

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
        bars = self._network.get_bars(symbol, timeframe=timeframe, limit=limit)
        if self._cache is not None:
            self._cache.write_bars(symbol, timeframe, bars)
        return bars

    def _cache_is_fresh(self, bars: list[Bar]) -> bool:
        """Check if cached bars include data up to at least the previous trading day."""
        if not bars:
            return False
        min_date = self._get_min_date()
        return bars[-1].timestamp.date() >= min_date

    def _get_min_date(self) -> date:
        """Return the minimum acceptable latest-bar date (lazy, computed once per instance)."""
        if self._min_date is not None:
            return self._min_date.date()
        now = datetime.now()
        d = now.date()
        # Before 09:30 on a weekday, the latest available data is from the
        # previous trading day's close; after 15:00 it's today.
        if now.hour < 16:
            d -= timedelta(days=1)
        # Skip weekends backwards
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        self._min_date = datetime.combine(d, datetime.min.time())
        return d


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
        for provider in real_providers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                budget_exhausted = True
                break
            try:
                bars = provider.get_bars(symbol, timeframe=timeframe, limit=limit)
                self.last_resolved_provider = provider.name
                if provider.name not in {"cache", "sample"} and cache_provider is not None:
                    cache_provider.write_bars(symbol, timeframe, bars)
                return bars
            except Exception as exc:
                self._record_failure(symbol, provider, exc)
                if time.monotonic() >= deadline:
                    budget_exhausted = True
                    break

        if budget_exhausted:
            raise ProviderError(
                f"{symbol}: data provider timeout budget exhausted after "
                f"{self.total_timeout:.1f}s for timeframe={timeframe}."
            )

        raise ProviderError(
            f"{symbol}: all data providers failed for timeframe={timeframe}. "
            f"Tried: {[p.name for p in real_providers]}. "
            "Check network or API keys."
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
