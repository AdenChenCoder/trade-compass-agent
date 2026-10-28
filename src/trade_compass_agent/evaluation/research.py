"""Versioned strategy hypotheses and immutable, reproducible research runs."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from trade_compass_agent import __version__
from trade_compass_agent.concurrency import atomic_write, file_transaction
from trade_compass_agent.data.metadata import bars_metadata
from trade_compass_agent.data.quality import DataQualityLayer
from trade_compass_agent.evaluation.backtester import ENGINE_VERSION, BacktestConfig, normalize_daily_prices, run_backtest


class BreakoutParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    fast_window: int = Field(10, ge=2, le=250)
    slow_window: int = Field(20, ge=3, le=500)
    breakout_window: int = Field(20, ge=2, le=250)
    volume_window: int = Field(10, ge=2, le=250)
    volume_ratio: float = Field(1.2, gt=0, le=10, allow_inf_nan=False)

    @model_validator(mode="after")
    def check_windows(self):
        if self.fast_window >= self.slow_window:
            raise ValueError("快均线窗口必须小于慢均线窗口")
        return self


class StrategyDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    strategy_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    market: Literal["CN", "US", "HK"] = "CN"
    hypothesis: str = Field(min_length=1, max_length=4000)
    invalidation: str = Field(min_length=1, max_length=4000)
    method: Literal["trend_breakout"] = "trend_breakout"
    parameters: BreakoutParameters = Field(default_factory=BreakoutParameters)
    sources: list[str] = Field(default_factory=list, max_length=30)
    knowledge_refs: list[str] = Field(default_factory=list, max_length=30)


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    version: str = Field(pattern=r"^[a-f0-9]{16}$")
    symbol: str = Field(pattern=r"^[0-9]{6}$")
    limit: int = Field(250, ge=30, le=1000)
    start_date: date | None = None
    end_date: date | None = None
    backtest: BacktestConfig | None = None

    @model_validator(mode="after")
    def check_dates(self):
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        if self.end_date and self.end_date >= today:
            raise ValueError("end_date must precede today; unfinished daily bars are excluded")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not follow end_date")
        return self


def _json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _digest(payload) -> str:
    return hashlib.sha256(_json(payload).encode()).hexdigest()


class ResearchStore:
    """Append new versions/runs; nothing reads or rewrites installed assets."""

    def __init__(self, data_dir: Path):
        self.root = data_dir / "research"

    def save_strategy(self, definition: StrategyDefinition) -> dict:
        payload = definition.model_dump(mode="json")
        version = _digest(payload)[:16]
        path = self.root / "strategies" / definition.strategy_id / f"{version}.json"
        with file_transaction(self.root / ".lock"):
            if path.exists():
                return self.get_strategy(definition.strategy_id, version)
            record = {"schema_version": 1, "version": version,
                      "created_at": datetime.now(timezone.utc).isoformat(), "definition": payload}
            atomic_write(path, _json(record) + "\n")
        return record

    def get_strategy(self, strategy_id: str, version: str) -> dict:
        # Reuse the request contract for path safety at every entry point.
        request = ResearchRequest(strategy_id=strategy_id, version=version, symbol="000001")
        path = self.root / "strategies" / request.strategy_id / f"{request.version}.json"
        if not path.is_file():
            raise ValueError("策略版本不存在")
        record = json.loads(path.read_text(encoding="utf-8"))
        definition = StrategyDefinition.model_validate(record["definition"])
        if definition.strategy_id != strategy_id or record["version"] != version or _digest(definition.model_dump(mode="json"))[:16] != version:
            raise ValueError("策略文件校验失败，请从备份恢复")
        return record

    def list_strategies(self) -> list[dict]:
        paths = (self.root / "strategies").glob("*/*.json")
        return sorted((self.get_strategy(path.parent.name, path.stem) for path in paths),
                      key=lambda record: record["created_at"], reverse=True)

    def save_run(self, record: dict) -> dict:
        run_id = uuid4().hex
        record = {**record, "run_id": run_id, "schema_version": 1,
                  "created_at": datetime.now(timezone.utc).isoformat()}
        record["record_sha256"] = _digest(record)
        with file_transaction(self.root / ".lock"):
            atomic_write(self.root / "runs" / f"{run_id}.json", _json(record) + "\n")
        return record

    def get_run(self, run_id: str) -> dict:
        if len(run_id) != 32 or any(char not in "0123456789abcdef" for char in run_id):
            raise ValueError("无效的研究记录 ID")
        path = self.root / "runs" / f"{run_id}.json"
        if not path.is_file():
            raise ValueError("研究记录不存在")
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["run_id"] != run_id or _digest({key: value for key, value in record.items() if key != "record_sha256"}) != record["record_sha256"]:
            raise ValueError("研究记录校验失败，请从备份恢复")
        if _digest(record["dataset"]) != record["dataset_sha256"]:
            raise ValueError("行情快照校验失败，请从备份恢复")
        return record

    def list_runs(self, limit: int = 50) -> list[dict]:
        records = [self.get_run(path.stem) for path in (self.root / "runs").glob("*.json")]
        records.sort(key=lambda record: record["created_at"], reverse=True)
        return [{key: value for key, value in record.items() if key not in {"dataset", "signals", "knowledge_snapshots"}}
                for record in records[:max(1, min(limit, 100))]]


def breakout_signals(frame: pd.DataFrame, parameters: BreakoutParameters) -> pd.Series:
    """Only current/past completed bars; prior box/volume excludes today's bar."""
    frame = normalize_daily_prices(frame)
    close = frame["close"]
    fast = close.rolling(parameters.fast_window).mean()
    slow = close.rolling(parameters.slow_window).mean()
    ceiling = frame["high"].shift(1).rolling(parameters.breakout_window).max()
    volume = frame["volume"].shift(1).rolling(parameters.volume_window).mean()
    ready = fast.notna() & slow.notna() & ceiling.notna() & volume.gt(0)
    enter = ready & close.gt(ceiling) & fast.gt(slow) & frame["volume"].ge(volume * parameters.volume_ratio)
    exit_signal = ready & (close.lt(slow) | fast.lt(slow))
    scores = pd.Series(0.5, index=frame.index)
    scores.loc[enter] = 1.0
    scores.loc[exit_signal] = 0.0
    return scores


def run_strategy_research(stack, request: ResearchRequest) -> dict:
    store = ResearchStore(stack.config.data_dir)
    strategy = store.get_strategy(request.strategy_id, request.version)
    definition = StrategyDefinition.model_validate(strategy["definition"])
    if definition.market != "CN":
        raise ValueError("当前回测仅支持 A 股；美股和港股假设可保存，需独立成交与费用模型后验证")
    bars = stack.provider.get_bars(request.symbol, timeframe="1d", limit=request.limit)
    provider = getattr(stack.provider, "last_resolved_provider", None) or getattr(stack.provider, "name", "unknown")
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    bars = [bar for bar in bars if bar.timestamp.date() < today
            and (request.end_date is None or bar.timestamp.date() <= request.end_date)]
    bars.sort(key=lambda bar: bar.timestamp)
    quality = DataQualityLayer().check_bars(bars)
    if not quality.ok:
        raise ValueError("行情质量不合格: " + "; ".join(quality.warnings))
    if any(not all(math.isfinite(value) and value > 0 for value in
                   (bar.open, bar.high, bar.low, bar.close)) or not math.isfinite(bar.volume) for bar in bars):
        raise ValueError("行情包含无效价格或成交量")
    required = max(definition.parameters.slow_window, definition.parameters.breakout_window + 1,
                   definition.parameters.volume_window + 1) + 1
    if len(bars) < required:
        raise ValueError(f"完整日线不足，至少需要 {required} 条（含预热和下一日成交）")
    if len({bar.adjusted for bar in bars}) > 1:
        raise ValueError("复权口径混杂，不能进行同一次回测")
    dataset = [{"date": bar.timestamp.isoformat(), "open": bar.open, "high": bar.high,
                "low": bar.low, "close": bar.close, "volume": bar.volume,
                "adjusted": bar.adjusted, "source": bar.source} for bar in bars]
    frame = normalize_daily_prices(pd.DataFrame(dataset))
    scores = breakout_signals(frame, definition.parameters)
    if request.start_date:
        scores = scores.loc[scores.index.date >= request.start_date]
    if len(scores) < 2:
        raise ValueError("所选区间不足两个完整交易日")
    costs = stack.config.trading_costs
    cfg = request.backtest or BacktestConfig(
        commission_rate=costs.commission_rate, min_commission=costs.min_commission,
        stamp_duty_rate=costs.stamp_duty_rate, transfer_fee_rate=costs.transfer_fee_rate,
        slippage_pct=costs.slippage_bps / 10_000, min_lot=costs.min_lot_size,
    )
    result = run_backtest(pd.DataFrame({request.symbol: scores}), {request.symbol: frame}, cfg)
    warnings = list(quality.warnings)
    warnings.append("探索性回测，未经样本外和前向模拟验证，不能证明未来收益。")
    warnings.append("日线不能还原排队、盘中流动性与停复牌细节；未建模分红及退市偏差。")
    if any(bar.adjusted for bar in bars):
        warnings.append("使用复权价格；无法据此还原历史真实成交金额及当时可见的复权因子。")
    metadata = bars_metadata(bars, provider, "1d")
    if metadata["is_sample"]:
        warnings.append("示例行情仅用于演示流程，不作为策略收益证据。")
    from trade_compass_agent.runtime.skills import discover_skills
    skills = {skill.name: skill for skill in discover_skills(memory_dir=stack.config.memory_dir)}
    snapshots = []
    for name in dict.fromkeys(["strategy-research", "strategy-validation-review", *definition.knowledge_refs]):
        skill = skills.get(name)
        if skill is None:
            warnings.append(f"知识引用 {name} 未找到可读取的 Skill，未保存其内容快照。")
            continue
        files = {"SKILL.md": skill.path.read_text(encoding="utf-8")}
        for path in sorted((skill.path.parent / "references").rglob("*.md")):
            files[str(path.relative_to(skill.path.parent))] = path.read_text(encoding="utf-8")
        snapshots.append({"name": name, "source": skill.source, "sha256": _digest(files), "files": files})
    return store.save_run({
        "strategy_id": request.strategy_id, "strategy_version": request.version,
        "strategy_snapshot": strategy, "symbol": request.symbol,
        "evidence_stage": "sample_demo" if metadata["is_sample"] else "exploratory_backtest",
        "app_version": __version__, "request": request.model_dump(mode="json"),
        "metadata": metadata, "dataset": dataset, "dataset_sha256": _digest(dataset),
        "knowledge_snapshots": snapshots,
        "signals": {stamp.strftime("%Y-%m-%d"): float(value) for stamp, value in scores.items()},
        "result": asdict(result), "warnings": warnings,
    })


def replay_research_run(record: dict) -> dict:
    """Recompute the saved run without a provider or fresh network data."""
    if _digest(record["dataset"]) != record["dataset_sha256"]:
        raise ValueError("行情快照校验失败")
    if record["result"]["engine_version"] != ENGINE_VERSION:
        raise ValueError("回测引擎版本不同，请使用原版本重放")
    signals = pd.DataFrame({record["symbol"]: record["signals"]})
    result = run_backtest(signals, {record["symbol"]: pd.DataFrame(record["dataset"])},
                          BacktestConfig(**record["result"]["config"]))
    return asdict(result)
