from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Protocol

from trade_compass_agent.domain import LimitUpSummary, MarketPulse, SectorStrength

from .network import extend_no_proxy_for_eastmoney, patch_requests_for_eastmoney, run_with_timeout, short_error_message
from .providers import DEFAULT_REQUEST_TIMEOUT, ProviderError


class MarketPulseProvider(Protocol):
    name: str

    def get_market_pulse(self) -> MarketPulse: ...


class SampleMarketPulseProvider:
    name = "sample"

    def get_market_pulse(self) -> MarketPulse:
        sectors = [
            SectorStrength(
                name="机器人",
                change_pct=2.6,
                turnover_pct=3.1,
                up_count=42,
                down_count=7,
                leader="Sample 300750",
                leader_change_pct=6.2,
            ),
            SectorStrength(
                name="半导体",
                change_pct=1.8,
                turnover_pct=2.4,
                up_count=36,
                down_count=12,
                leader="Sample 512690",
                leader_change_pct=3.4,
            ),
            SectorStrength(
                name="白酒",
                change_pct=-0.4,
                turnover_pct=1.1,
                up_count=8,
                down_count=23,
                leader="Sample 600519",
                leader_change_pct=0.8,
            ),
        ]
        return MarketPulse(
            timestamp=datetime.now(),
            provider_name=self.name,
            sectors=sectors,
            limit_up=LimitUpSummary(
                count=38,
                strong_count=9,
                top_industries=["机器人", "半导体", "低空经济"],
                leaders=["Sample 300750", "Sample 000001"],
            ),
            notes=["样例市场脉搏：用于离线开发和上游不可用时的 UI/流程验证。"],
        )


class AkshareMarketPulseProvider:
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

    def get_market_pulse(self) -> MarketPulse:
        warnings: list[str] = []
        sectors: list[SectorStrength] = []
        limit_up = LimitUpSummary(count=0, strong_count=0, top_industries=[], leaders=[])

        try:
            sectors = self._fetch_sector_strength()
        except Exception as exc:
            warnings.append(f"行业强度暂不可用：{short_error_message(exc)}")

        try:
            limit_up = self._fetch_limit_up_summary()
        except Exception as exc:
            warnings.append(f"涨停池暂不可用：{short_error_message(exc)}")

        if not sectors and limit_up.count == 0:
            raise ProviderError("; ".join(warnings) or "market pulse unavailable")

        notes = self._build_notes(sectors, limit_up)
        return MarketPulse(
            timestamp=datetime.now(),
            provider_name=self.name,
            sectors=sectors,
            limit_up=limit_up,
            notes=notes,
            warnings=warnings,
        )

    def _fetch_sector_strength(self) -> list[SectorStrength]:
        def fetch():
            return self.ak.stock_board_industry_name_em()

        df = run_with_timeout(fetch, self.timeout + 2, "akshare industry board")
        if df is None or getattr(df, "empty", True):
            return []

        sectors: list[SectorStrength] = []
        for _, row in df.sort_values("涨跌幅", ascending=False).head(8).iterrows():
            sectors.append(
                SectorStrength(
                    name=str(row.get("板块名称", "")),
                    change_pct=_float(row.get("涨跌幅")),
                    turnover_pct=_optional_float(row.get("换手率")),
                    up_count=_optional_int(row.get("上涨家数")),
                    down_count=_optional_int(row.get("下跌家数")),
                    leader=str(row.get("领涨股票", "")) or None,
                    leader_change_pct=_optional_float(row.get("领涨股票-涨跌幅")),
                )
            )
        return sectors

    def _fetch_limit_up_summary(self) -> LimitUpSummary:
        today = datetime.now().strftime("%Y%m%d")

        def fetch_pool():
            return self.ak.stock_zt_pool_em(date=today)

        def fetch_strong_pool():
            return self.ak.stock_zt_pool_strong_em(date=today)

        pool = run_with_timeout(fetch_pool, self.timeout + 2, "akshare limit-up pool")
        strong = run_with_timeout(fetch_strong_pool, self.timeout + 2, "akshare strong limit-up pool")
        if pool is None or getattr(pool, "empty", True):
            return LimitUpSummary(count=0, strong_count=0, top_industries=[], leaders=[])

        industries = Counter(str(value) for value in pool.get("所属行业", []) if str(value))
        leaders = [
            f"{row.get('名称', '')}({row.get('连板数', 1)}板)"
            for _, row in pool.sort_values(["连板数", "封板资金"], ascending=False).head(5).iterrows()
        ]
        strong_count = 0 if strong is None or getattr(strong, "empty", True) else len(strong)
        return LimitUpSummary(
            count=len(pool),
            strong_count=strong_count,
            top_industries=[name for name, _ in industries.most_common(5)],
            leaders=leaders,
        )

    def _build_notes(self, sectors: list[SectorStrength], limit_up: LimitUpSummary) -> list[str]:
        notes: list[str] = []
        if sectors:
            top = sectors[0]
            notes.append(f"最强行业：{top.name} {top.change_pct:.2f}%，领涨 {top.leader or 'n/a'}。")
        if limit_up.count:
            notes.append(
                f"涨停池 {limit_up.count} 只，强势池 {limit_up.strong_count} 只，"
                f"高频行业：{', '.join(limit_up.top_industries[:3]) or 'n/a'}。"
            )
        if not notes:
            notes.append("市场脉搏数据不足，盘前信号置信度应下调。")
        return notes


class FallbackMarketPulseProvider:
    """Tries primary; propagates error if it fails (no silent sample fallback)."""

    name = "auto"

    def __init__(self, primary: MarketPulseProvider) -> None:
        self.primary = primary
        self.last_warnings: list[str] = []

    def get_market_pulse(self) -> MarketPulse:
        return self.primary.get_market_pulse()


def create_market_pulse_provider(name: str = "auto") -> MarketPulseProvider:
    normalized = name.lower().strip()
    if normalized == "sample":
        return SampleMarketPulseProvider()
    if normalized == "akshare":
        return AkshareMarketPulseProvider()
    if normalized in {"auto", "baostock", "sina", "tushare"}:
        return AkshareMarketPulseProvider()
    return AkshareMarketPulseProvider()


def _float(value) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _optional_float(value) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _optional_int(value) -> int | None:
    try:
        return int(value)
    except Exception:
        return None
