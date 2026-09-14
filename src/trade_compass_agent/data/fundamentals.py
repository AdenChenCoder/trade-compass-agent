from __future__ import annotations

import os
import math

import requests
from dataclasses import dataclass, replace
from statistics import mean
from typing import Protocol

from trade_compass_agent.domain import Bar

from .network import extend_no_proxy_for_eastmoney, patch_requests_for_eastmoney, rate_limit_domain, run_with_timeout, short_error_message
from .providers import DEFAULT_REQUEST_TIMEOUT, ProviderError


@dataclass(frozen=True)
class FundamentalsSnapshot:
    symbol: str
    pe_ttm: float | None = None
    pb: float | None = None
    market_cap: float | None = None
    roe: float | None = None
    float_shares: float | None = None
    total_shares: float | None = None
    industry: str | None = None
    provider_name: str = "unknown"
    notes: tuple[str, ...] = ()
    as_of: str | None = None

    def __post_init__(self):
        invalid = []
        for field in ("pe_ttm", "pb", "market_cap", "roe", "float_shares", "total_shares"):
            value = getattr(self, field)
            if value is None:
                continue
            number = _safe_float(value)
            if (number is None or (field in {"market_cap", "float_shares", "total_shares"} and number <= 0)
                    or (field in {"pe_ttm", "pb"} and number == 0)):
                object.__setattr__(self, field, None)
                invalid.append(field)
        if self.industry is not None and (not str(self.industry).strip() or _safe_float(self.industry) is not None
                                          or str(self.industry).strip() in {"-", "--", "nan", "None"}):
            object.__setattr__(self, "industry", None)
            invalid.append("industry")
        if _not_corporate(self.symbol):
            for field in ("pe_ttm", "pb", "market_cap", "roe", "float_shares", "total_shares", "industry"):
                object.__setattr__(self, field, None)
            object.__setattr__(self, "notes", (*self.notes, "该资产不适用个股企业基本面；基金需使用基金规模、净值及持仓口径。"))
        elif invalid:
            object.__setattr__(self, "notes", (*self.notes, "已剔除无效基本面字段：" + ", ".join(invalid)))

    @property
    def data_status(self):
        return "not_applicable" if _not_corporate(self.symbol) else "available" if self.has_real_fundamentals else "unavailable"

    @property
    def has_real_fundamentals(self) -> bool:
        return not _not_corporate(self.symbol) and any(value is not None for value in (self.pe_ttm, self.pb, self.market_cap, self.roe))


class FundamentalsProvider(Protocol):
    name: str

    def get_snapshot(self, symbol: str, *, bars: list[Bar] | None = None) -> FundamentalsSnapshot: ...


class RuleFundamentalsProvider:
    """Derive price/volume context from bars when real fundamentals are unavailable."""

    name = "rule"

    def get_snapshot(self, symbol: str, *, bars: list[Bar] | None = None) -> FundamentalsSnapshot:
        notes: list[str] = []
        if not bars:
            return FundamentalsSnapshot(
                symbol=symbol,
                provider_name=self.name,
                notes=("无 K 线数据，仅返回空基本面占位。",),
            )

        window = bars[-252:] if len(bars) >= 252 else bars
        highs = [bar.high for bar in window]
        lows = [bar.low for bar in window]
        volumes = [bar.volume for bar in window]
        high_52w = max(highs) if highs else None
        low_52w = min(lows) if lows else None
        avg_volume = mean(volumes[-20:]) if volumes else None
        last = bars[-1].close

        if high_52w is not None and low_52w is not None:
            notes.append(f"52w_high={high_52w:.2f}")
            notes.append(f"52w_low={low_52w:.2f}")
            if high_52w:
                notes.append(f"dist_52w_high={(last / high_52w - 1):+.2%}")
        if avg_volume is not None:
            notes.append(f"avg_volume_20d={avg_volume:,.0f}")
        notes.append("bar-derived only; PE/PB/market cap unavailable")

        return FundamentalsSnapshot(symbol=symbol, provider_name=self.name, notes=tuple(notes))


class EastmoneyDirectFundamentalsProvider:
    """Direct HTTP to push2.eastmoney.com — avoids akshare wrapper failures."""

    name = "eastmoney_direct"

    def __init__(self, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> None:
        extend_no_proxy_for_eastmoney()
        self.timeout = timeout

    def get_snapshot(self, symbol: str, *, bars: list[Bar] | None = None) -> FundamentalsSnapshot:
        normalized = symbol.strip()
        if _not_corporate(normalized):
            return FundamentalsSnapshot(symbol=normalized, provider_name=self.name)
        from .providers import split_symbol
        market, code = split_symbol(normalized)
        secid = f"{0 if market == 'sz' else 1}.{code}"
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        params = {
            "secid": secid,
            "fields": "f57,f58,f84,f85,f116,f117,f124,f127,f162,f167,f168",
            "ut": "fa5fd1943c7b386f172d6893dbfbaeb",
        }

        def fetch():
            rate_limit_domain(url)
            last_exc = None
            for attempt in range(2):
                try:
                    resp = requests.get(
                        url,
                        params=params,
                        timeout=self.timeout,
                        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.eastmoney.com"},
                        proxies={"http": None, "https": None},
                    )
                    resp.raise_for_status()
                    return resp.json()
                except Exception as exc:
                    last_exc = exc
                    if attempt == 0:
                        import time
                        time.sleep(0.3)
            raise last_exc  # type: ignore[misc]

        try:
            data = run_with_timeout(fetch, self.timeout, f"em fundamentals {normalized}")
        except Exception as exc:
            raise ProviderError(f"eastmoney direct failed: {short_error_message(exc)}") from exc

        quote = (data or {}).get("data") or {}
        if not quote:
            raise ProviderError("eastmoney direct returned empty data")

        # Eastmoney push2 numeric fields are scaled x100 for PE/PB/turnover
        pe = _scale_em_field(quote.get("f162"))
        pb = _scale_em_field(quote.get("f167"))
        market_cap = _safe_float(quote.get("f116"))
        float_cap = _safe_float(quote.get("f117"))
        float_shares = _safe_float(quote.get("f85"))
        total_shares = _safe_float(quote.get("f84"))
        industry = str(quote.get("f127") or "").strip() or None

        if pe is None and pb is None and market_cap is None:
            raise ProviderError("eastmoney direct returned no PE/PB fields")

        return FundamentalsSnapshot(
            symbol=normalized,
            pe_ttm=pe,
            pb=pb,
            market_cap=market_cap or float_cap,
            float_shares=float_shares,
            total_shares=total_shares,
            industry=industry,
            provider_name=self.name,
        )


class AkshareFundamentalsProvider:
    name = "akshare"

    def __init__(self, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> None:
        extend_no_proxy_for_eastmoney()
        patch_requests_for_eastmoney(timeout)
        try:
            import akshare as ak  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("akshare is not available") from exc
        self.ak = ak
        self.timeout = timeout
        self._fallback = RuleFundamentalsProvider()

    def get_snapshot(self, symbol: str, *, bars: list[Bar] | None = None) -> FundamentalsSnapshot:
        normalized = symbol.strip()
        if _not_corporate(normalized):
            return FundamentalsSnapshot(symbol=normalized, provider_name=self.name)

        def fetch():
            if hasattr(self.ak, "stock_individual_info_em"):
                return self.ak.stock_individual_info_em(symbol=normalized)
            raise ProviderError("stock_individual_info_em unavailable")

        try:
            df = run_with_timeout(fetch, self.timeout + 2, f"fundamentals {normalized}")
        except Exception as exc:
            fallback = self._fallback.get_snapshot(normalized, bars=bars)
            note = f"akshare fundamentals unavailable: {short_error_message(exc)}"
            return FundamentalsSnapshot(
                symbol=fallback.symbol,
                pe_ttm=fallback.pe_ttm,
                pb=fallback.pb,
                market_cap=fallback.market_cap,
                roe=fallback.roe,
                provider_name=f"{self.name}/{fallback.provider_name}",
                notes=(*fallback.notes, note),
            )

        mapping = {}
        if df is not None and not getattr(df, "empty", True):
            item_col = "item" if "item" in df.columns else df.columns[0]
            value_col = "value" if "value" in df.columns else df.columns[1]
            for _, row in df.iterrows():
                mapping[str(row[item_col]).strip()] = row[value_col]

        pe = _pick_float(mapping, ("市盈率-动态", "市盈率(TTM)", "市盈率", "PE"))
        pb = _pick_float(mapping, ("市净率", "PB"))
        market_cap = _pick_float(mapping, ("总市值", "流通市值"))
        roe = _pick_float(mapping, ("净资产收益率", "ROE"))
        float_shares = _pick_float(mapping, ("流通股", "流通股本"))
        total_shares = _pick_float(mapping, ("总股本",))
        industry = _pick_str(mapping, ("行业", "所处行业"))

        if pe is None and pb is None and market_cap is None and roe is None:
            fallback = self._fallback.get_snapshot(normalized, bars=bars)
            return FundamentalsSnapshot(
                symbol=normalized,
                provider_name=f"{self.name}/{fallback.provider_name}",
                notes=(*fallback.notes, "akshare info returned no PE/PB fields"),
            )

        return FundamentalsSnapshot(
            symbol=normalized,
            pe_ttm=pe,
            pb=pb,
            market_cap=market_cap,
            roe=roe,
            float_shares=float_shares,
            total_shares=total_shares,
            industry=industry,
            provider_name=self.name,
        )


class TushareFundamentalsProvider:
    name = "tushare"

    def __init__(self, token_env: str = "TUSHARE_TOKEN", timeout: float = DEFAULT_REQUEST_TIMEOUT) -> None:
        token = os.getenv(token_env, "").strip()
        if not token:
            raise RuntimeError(f"{token_env} not set")
        self._token = token
        self.timeout = timeout
        self._fallback = RuleFundamentalsProvider()

    def get_snapshots(self, symbols: list[str]) -> dict[str, FundamentalsSnapshot]:
        from trade_compass_agent.domain import InstrumentKind
        from .providers import _market_now, _prev_trading_date, infer_instrument_kind, is_index_symbol, is_lof_symbol
        from .tushare_provider import query_tushare, to_ts_code

        codes = {to_ts_code(s): s for s in symbols if infer_instrument_kind(s) != InstrumentKind.ETF
                 and not is_index_symbol(s) and not is_lof_symbol(s)}
        if not codes:
            return {}
        now = _market_now()
        day = _prev_trading_date(now.date(), now.hour).strftime("%Y%m%d")
        timeout = max(self.timeout, 6)
        frame = run_with_timeout(
            lambda: query_tushare("daily_basic", token=self._token, timeout=timeout, trade_date=day,
                                  limit=6000, fields="ts_code,trade_date,pe_ttm,pb,total_mv"),
            timeout + 1, "tushare fundamentals batch",
        )
        if frame.empty:
            return {}
        if len(frame) >= 6000 or not (frame.trade_date == day).all() or frame.ts_code.duplicated().any():
            raise ProviderError("Tushare fundamental batch is incomplete or has invalid identities/dates")
        result = {}
        for _, row in frame[frame.ts_code.isin(codes)].iterrows():
            symbol = codes[row.ts_code]
            total_mv = _safe_float(row.get("total_mv"))
            snap = FundamentalsSnapshot(symbol=symbol, pe_ttm=_safe_float(row.get("pe_ttm")),
                                        pb=_safe_float(row.get("pb")),
                                        market_cap=total_mv * 10_000 if total_mv is not None else None,
                                        provider_name=self.name, as_of=str(row["trade_date"]))
            if snap.has_real_fundamentals:
                result[symbol] = snap
        return result

    def get_snapshot(self, symbol: str, *, bars: list[Bar] | None = None) -> FundamentalsSnapshot:
        from .tushare_provider import query_tushare, to_ts_code

        if _not_corporate(symbol):
            return FundamentalsSnapshot(symbol=symbol, provider_name=self.name)
        ts_code = to_ts_code(symbol)

        def fetch():
            return query_tushare(
                "daily_basic", token=self._token, timeout=self.timeout,
                ts_code=ts_code, limit=1, fields="ts_code,trade_date,pe_ttm,pb,total_mv",
            )

        try:
            df = run_with_timeout(fetch, self.timeout + 2, f"tushare fundamentals {symbol}")
        except Exception as exc:
            fallback = self._fallback.get_snapshot(symbol, bars=bars)
            return FundamentalsSnapshot(
                symbol=fallback.symbol,
                provider_name=f"{self.name}/{fallback.provider_name}",
                notes=(*fallback.notes, f"tushare fundamentals unavailable: {short_error_message(exc)}"),
            )

        if df is None or getattr(df, "empty", True):
            fallback = self._fallback.get_snapshot(symbol, bars=bars)
            return FundamentalsSnapshot(
                symbol=symbol,
                provider_name=f"{self.name}/{fallback.provider_name}",
                notes=(*fallback.notes, "tushare daily_basic empty"),
            )

        row = df.sort_values("trade_date").iloc[-1]
        total_mv = _safe_float(row.get("total_mv"))
        market_cap = total_mv * 10_000 if total_mv is not None else None
        return FundamentalsSnapshot(
            symbol=symbol,
            pe_ttm=_safe_float(row.get("pe_ttm")),
            pb=_safe_float(row.get("pb")),
            market_cap=market_cap,
            provider_name=self.name, as_of=str(row["trade_date"]),
        )


class ChainFundamentalsProvider:
    """Try real providers in order; always fall back to bar-derived rule snapshot."""

    name = "auto"

    def __init__(self, providers: list[FundamentalsProvider]) -> None:
        self.providers = providers or [RuleFundamentalsProvider()]
        self._rule = RuleFundamentalsProvider()

    def get_snapshot(self, symbol: str, *, bars: list[Bar] | None = None) -> FundamentalsSnapshot:
        if _not_corporate(symbol):
            return self._rule.get_snapshot(symbol, bars=bars)
        failures = []
        for provider in self.providers:
            if provider.name == "rule":
                continue
            try:
                snapshot = provider.get_snapshot(symbol, bars=bars)
                if snapshot.symbol != symbol:
                    raise ProviderError("fundamental symbol mismatch")
                if snapshot.has_real_fundamentals:
                    return replace(snapshot, notes=(*snapshot.notes, *failures))
                failures.extend(snapshot.notes or (f"{provider.name}: no applicable fundamentals",))
            except Exception as exc:
                failures.append(f"{provider.name}: {short_error_message(exc)}")
        fallback = self._rule.get_snapshot(symbol, bars=bars)
        return replace(fallback, notes=(*fallback.notes, *failures))



def create_fundamentals_provider(
    *,
    tushare_enabled: bool = False,
    tushare_token_env: str = "TUSHARE_TOKEN",
    timeout: float = DEFAULT_REQUEST_TIMEOUT,
) -> FundamentalsProvider:
    providers: list[FundamentalsProvider] = []
    if tushare_enabled and os.getenv(tushare_token_env, "").strip():
        try:
            providers.append(TushareFundamentalsProvider(token_env=tushare_token_env, timeout=timeout))
        except Exception:
            pass
    try:
        providers.append(EastmoneyDirectFundamentalsProvider(timeout=timeout))
    except Exception:
        pass
    try:
        providers.append(AkshareFundamentalsProvider(timeout=timeout))
    except Exception:
        pass
    providers.append(RuleFundamentalsProvider())
    return ChainFundamentalsProvider(providers)


def _pick_float(mapping: dict[str, object], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key in mapping:
            value = _safe_float(mapping[key])
            if value is not None:
                return value
    return None


def _pick_str(mapping: dict[str, object], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if key in mapping:
            val = mapping[key]
            if val is not None and str(val).strip() not in ("", "--", "-", "nan", "None"):
                return str(val).strip()
    return None


def _scale_em_field(value) -> float | None:
    """Eastmoney push2 returns PE/PB/turnover as integer x100."""
    raw = _safe_float(value)
    if raw is None:
        return None
    return round(raw / 100.0, 4)


def _safe_float(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text or text in {"--", "-", "nan", "None"}:
        return None
    try:
        number = float(text)
        return number if math.isfinite(number) else None
    except ValueError:
        return None


def _not_corporate(symbol: str) -> bool:
    from trade_compass_agent.domain import InstrumentKind
    from .providers import infer_instrument_kind, is_index_symbol, is_lof_symbol
    return infer_instrument_kind(symbol) == InstrumentKind.ETF or is_index_symbol(symbol) or is_lof_symbol(symbol)
