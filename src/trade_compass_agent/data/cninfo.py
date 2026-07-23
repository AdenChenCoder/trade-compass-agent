from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Protocol

from trade_compass_agent.domain import Event

from .network import extend_no_proxy_for_eastmoney, patch_requests_for_eastmoney, run_with_timeout, short_error_message
from .providers import DEFAULT_REQUEST_TIMEOUT, ProviderError

_SYMBOL_RE = re.compile(r"(\d{6})")


class CninfoProvider(Protocol):
    name: str

    def get_events(self, symbol: str, limit: int = 5) -> list[Event]: ...


class SampleCninfoProvider:
    """Deterministic offline announcements for development and tests."""

    name = "sample"

    def get_events(self, symbol: str, limit: int = 5) -> list[Event]:
        now = datetime.now().replace(hour=15, minute=0, second=0, microsecond=0)
        templates = [
            ("定期报告", "2024 年年度报告摘要"),
            ("重大事项", "关于召开年度股东大会的通知"),
            ("经营数据", "2024 年度主要经营数据公告"),
            ("股权变动", "股东减持计划进展公告"),
            ("监管问询", "收到交易所问询函的公告"),
        ]
        events: list[Event] = []
        for idx, (event_type, title) in enumerate(templates[:limit]):
            events.append(
                Event(
                    symbol=symbol,
                    event_type=event_type,
                    title=f"[样例] {title}",
                    timestamp=now.replace(day=max(1, now.day - idx)),
                    source="sample/cninfo",
                    payload={"warning": "样例公告，非真实披露数据"},
                )
            )
        return events


class AkshareCninfoProvider:
    """Best-effort listed-company announcements via akshare (East Money / 巨潮)."""

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

    def get_events(self, symbol: str, limit: int = 5) -> list[Event]:
        normalized = _normalize_a_share_symbol(symbol)

        def fetch():
            end = datetime.now()
            start = end - timedelta(days=365)
            begin_date = start.strftime("%Y%m%d")
            end_date = end.strftime("%Y%m%d")

            if hasattr(self.ak, "stock_individual_notice_report"):
                return self.ak.stock_individual_notice_report(
                    security=normalized,
                    symbol="全部",
                    begin_date=begin_date,
                    end_date=end_date,
                )
            if hasattr(self.ak, "stock_zh_a_disclosure_report_cninfo"):
                return self.ak.stock_zh_a_disclosure_report_cninfo(
                    symbol=normalized,
                    start_date=begin_date,
                    end_date=end_date,
                )
            raise ProviderError("no akshare cninfo function available")

        try:
            df = run_with_timeout(fetch, self.timeout + 2, f"cninfo {normalized}")
        except Exception as exc:
            raise ProviderError(f"cninfo failed for {normalized}: {short_error_message(exc)}") from exc

        if df is None or getattr(df, "empty", True):
            raise ProviderError(f"no announcements for {normalized}")

        events: list[Event] = []
        for _, row in df.head(limit).iterrows():
            title = (
                row.get("公告标题")
                or row.get("title")
                or row.get("公告名称")
                or row.get("名称")
                or "未命名公告"
            )
            date_value = (
                row.get("公告日期")
                or row.get("公告时间")
                or row.get("date")
                or row.get("发布时间")
            )
            event_type = str(row.get("公告类型") or row.get("category") or "公告")
            timestamp = _parse_timestamp(date_value)
            events.append(
                Event(
                    symbol=normalized,
                    event_type=str(event_type),
                    title=str(title).strip(),
                    timestamp=timestamp,
                    source="akshare/cninfo",
                    payload={"provider": self.name},
                )
            )
        if not events:
            raise ProviderError(f"no announcements parsed for {normalized}")
        return events


class StubCninfoProvider:
    """Structured stub when live cninfo is unavailable."""

    name = "stub"

    def get_events(self, symbol: str, limit: int = 5) -> list[Event]:
        return [
            Event(
                symbol=symbol,
                event_type="stub",
                title="公告数据源暂不可用（已回退 stub）",
                timestamp=datetime.now(),
                source="stub/cninfo",
                payload={
                    "warning": "未接入真实公告 API；请安装 akshare 并检查网络。",
                    "limit": limit,
                },
            )
        ]


def create_cninfo_provider(name: str = "auto", *, timeout: float = DEFAULT_REQUEST_TIMEOUT) -> CninfoProvider:
    normalized = name.lower().strip()
    if normalized == "sample":
        return SampleCninfoProvider()
    if normalized == "stub":
        return StubCninfoProvider()
    if normalized == "akshare":
        return AkshareCninfoProvider(timeout=timeout)
    if normalized == "auto":
        return AkshareCninfoProvider(timeout=timeout)
    raise ValueError(f"Unknown cninfo provider: {name}")


def _normalize_a_share_symbol(symbol: str) -> str:
    text = symbol.strip()
    if not text:
        raise ProviderError("empty symbol")
    match = _SYMBOL_RE.search(text)
    if match:
        return match.group(1)
    raise ProviderError(f"invalid A-share symbol: {symbol}")


def _parse_timestamp(value) -> datetime:
    if value is None:
        return datetime.now()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.now()
