"""Evidence-bound reports: source values and arithmetic are owned by the runtime.

This module does not infer facts from prose. A report selects typed observations
and operations; the renderer supplies their identities, values, dates and units.
Free-form reasoning is reviewed separately and cannot rewrite observations.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field


SECTION_LABELS = {"overview": "概览", "market": "市场", "positions": "持仓",
                  "assessment": "研判", "plan": "条件计划", "execution": "执行记录",
                  "gaps": "待核实事项"}
Section = Literal["overview", "market", "positions", "assessment", "plan", "execution", "gaps"]


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=80)
    section: Section


class Observation(Item):
    type: Literal["fact"]
    ref: str = Field(description="事实目录中的id，逐字复制，不能引用报告条目id")


class Calculation(Item):
    type: Literal["calculation"]
    operation: Literal["sum", "difference", "change_pct", "share_pct", "compare"] = Field(description=(
        "sum:同指标、时点、口径且不重复的标的/账户；difference/compare:同标的、账户、时点和兼容指标；"
        "change_pct:同标的先后有序的价格；share_pct:同账户同一时点的market_value/account_capital或capital，"
        "或同回执的total_market_value/total_account_capital。所有操作数必须是原始事实，不能引用报告中的合计。"))
    refs: list[str] = Field(min_length=2, max_length=30, description="仅引用事实目录id，不引用报告条目id")


class Condition(Item):
    type: Literal["condition"]
    ref: str = Field(description="事实目录中的id，不是报告条目id")
    comparison: Literal["lt", "le", "gt", "ge"]
    threshold: str = Field(min_length=1, max_length=40)
    unit: str
    action: Literal["review", "consider_buy", "consider_sell", "wait"]
    quantity: int | None = Field(default=None, gt=0)


class Inference(Item):
    type: Literal["inference"]
    text: str = Field(min_length=1, max_length=2500)
    refs: list[str] = Field(min_length=1, max_length=20, description="引用事实目录id或本报告中fact/calculation条目的id；条件和其他研判不是证据")


class Gap(Item):
    type: Literal["gap"]
    text: str = Field(min_length=1, max_length=1000)


ReportItem = Annotated[Observation | Calculation | Condition | Inference | Gap,
                       Field(discriminator="type")]


class ReportDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    items: list[ReportItem] = Field(max_length=120)


class ReportContentDocument(ReportDocument):
    """Without execution records, an empty report cannot answer the task."""
    items: list[ReportItem] = Field(min_length=1, max_length=120)


@dataclass(frozen=True)
class Finding:
    category: str
    code: str
    message: str
    item_id: str = ""
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class Fact:
    id: str
    receipt_id: str
    pointer: str
    subject: str
    account: str
    metric: str
    label: str
    value: str
    unit: str
    observed_at: str | None
    kind: str
    source: str
    entity: str = ""
    timeframe: str = ""
    scope: str = ""

    def display(self) -> str:
        context = " · ".join(x for x in (self.subject, self.account) if x)
        stamp = self.observed_at or "时点未提供"
        details = json.loads(self.scope) if self.scope else {}
        labels = {"timeframe": "周期", "adjustment": "复权口径", "classification": "分类来源",
                  "url": "来源地址", "market_date": "市场日期", "category": "资金类别"}
        notes = [f"{labels[k]}：{v}" for k, v in details.items() if k in labels]
        if details.get("adjusted") is not None:
            notes.append("复权数据" if details["adjusted"] else "未复权数据")
        basis = details.get("volume_basis")
        if isinstance(basis, dict):
            notes.append(f"窗口{basis.get('window_start', '?')}至{basis.get('window_end', '?')}，"
                         f"{basis.get('bar_count', '?')}根K线，"
                         + ("包含当期" if basis.get("includes_current_bar") is True else "当期是否计入未确认"))
        if details.get("pnl_basis") == "fifo_after_entry_and_exit_fees":
            notes.append("FIFO净结算，已扣分摊买卖费用")
        scope = "；" + "；".join(notes) if notes else ""
        return f"{context + '：' if context else ''}{self.label} {self.value}{self.unit}（{stamp}{scope}；{self.source}）"


# These are producer field contracts, not phrases recognized in generated prose.
# Unknown fields remain in the raw receipt; they cannot be silently promoted to
# a numeric fact with a guessed unit or business meaning.
FIELDS = {
    "name": ("名称", "", "identity"), "symbol": ("代码", "", "identity"),
    "price": ("报价", "元/股", "price"), "last_price": ("持仓估值价", "元/股", "price"),
    "avg_cost": ("持仓成本价", "元/股", "cost"), "entry": ("买入成交价", "元/股", "entry"),
    "exit": ("卖出成交价", "元/股", "exit"), "open": ("开盘价", "元/股", "price"),
    "high": ("最高价", "元/股", "price"), "low": ("最低价", "元/股", "price"),
    "close": ("收盘价", "元/股", "price"), "prev_close": ("前收盘价", "元/股", "price"),
    "quantity": ("数量", "股", "quantity"), "volume": ("成交量", "", "volume"),
    "market_value": ("持仓市值", "元", "money"), "cost_basis": ("持仓成本", "元", "money"),
    "unrealized_pnl": ("浮动盈亏", "元", "money"), "realized_pnl": ("已实现净盈亏", "元", "settlement"),
    "pnl": ("结算净盈亏", "元", "settlement"), "fees": ("费用", "元", "money"),
    "available_cash": ("可用现金", "元", "cash"), "capital": ("账户额度", "元", "capital"),
    "total_account_capital": ("账户总额度", "元", "capital"),
    "account_capital": ("账户额度", "元", "capital"),
    "total_market_value": ("总持仓市值", "元", "money"),
    "total_available_cash": ("总可用现金", "元", "cash"),
    "total_trade_count": ("本地账本成交总数", "笔", "count"),
    "total_positions": ("持仓数", "只", "count"),
    "account_capital_pct": ("占账户额度比例", "%", "percentage"),
    "total_capital_pct": ("占全部账户额度比例", "%", "percentage"),
    "invested_mix_pct": ("占已投资市值比例", "%", "percentage"),
    "net_inflow_yi": ("净流入", "亿元", "flow"),
    "pnl_pct": ("持仓盈亏比例", "%", "percentage"), "change_pct": ("涨跌幅", "%", "percentage"),
    "turnover_pct": ("换手率", "%", "percentage"),
    "volume_ratio": ("量比", "倍", "ratio"), "current_rsi": ("RSI", "", "indicator"),
    "pe_ttm": ("市盈率TTM", "倍", "ratio"), "pb": ("市净率", "倍", "ratio"),
    "roe": ("净资产收益率", "%", "percentage"),
    "macd": ("MACD", "", "indicator"), "signal": ("信号线", "", "indicator"),
    "histogram": ("MACD柱", "", "indicator"),
    "upper": ("布林上轨", "元/股", "moving_average"),
    "middle": ("布林中轨", "元/股", "moving_average"),
    "lower": ("布林下轨", "元/股", "moving_average"),
    "current_volume": ("当期成交量", "", "volume"),
    "avg_volume": ("窗口平均成交量", "", "volume"),
    "count": ("返回数量", "条", "count"), "returned_count": ("返回明细数量", "条", "count"),
    "position_count": ("持仓数", "只", "count"),
    "title": ("来源标题", "", "quotation"), "content": ("来源原文", "", "quotation"),
    "snippet": ("来源摘要", "", "quotation"), "excerpt": ("来源原文", "", "quotation"),
    "text": ("来源原文", "", "quotation"),
}
EVIDENCE_TOOLS = frozenset({
    "analyze_portfolio", "get_bars", "batch_get_bars", "sina_realtime_quote",
    "get_market_pulse", "get_market_constraints", "get_fund_flow", "get_chip_distribution",
    "get_fundamentals", "batch_get_fundamentals", "compute_ma", "compute_macd", "compute_rsi",
    "compute_bollinger", "compute_volume_ratio", "fetch_url", "web_search", "search_market_flash",
    "search_stock_news", "batch_search_news", "search_announcements", "search_concept_boards", "get_events",
})
PORTFOLIO_OPERATIONS = {"builtin.check_positions", "builtin.refresh_market_prices", "builtin.scan_portfolio_exits"}
_NON_OBSERVATIONS = {"query", "arguments", "params", "prompt", "analysis", "summary",
                     "reasoning", "warnings", "error", "retry_hint", "hint", "note",
                     "interpretation", "task", "available", "_meta", "metadata"}


def _number(value) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("布尔值不是数值")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("数值格式无效") from exc
    if not result.is_finite():
        raise ValueError("数值不可用")
    return result


def _pointer(token) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def observation_time(value, zone="Asia/Shanghai"):
    """Normalize observations, without promoting request times to observations."""
    if not isinstance(value, str):
        return None
    try:
        if len(value) == 10:
            return date.fromisoformat(value).isoformat()
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=ZoneInfo(zone))
        return stamp.astimezone(timezone.utc).isoformat()
    except (ValueError, KeyError):
        return None


@dataclass
class EvidenceCatalog:
    facts: dict[str, Fact] = field(default_factory=dict)
    receipts: list[dict] = field(default_factory=list)

    @classmethod
    def build(cls, tool_results, *, run_scope: str):
        catalog = cls()
        for index, (tool, raw) in enumerate(tool_results):
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            digest = hashlib.sha256(raw.encode()).hexdigest()
            rid = hashlib.sha256(f"{run_scope}:{index}:{tool}:{digest}".encode()).hexdigest()[:24]
            catalog.receipts.append({"id": rid, "tool": tool, "sha256": digest, "result": raw})
            if tool == "dispatch_specialists":
                from trade_compass_agent.runtime.tool_receipts import dispatched_receipts
                nested = cls.build(dispatched_receipts(data), run_scope=rid)
                catalog.facts.update(nested.facts)
                catalog.receipts.extend(nested.receipts)
            if tool == "task_run_ledger":
                catalog._ledger(data, rid)
            if tool in PORTFOLIO_OPERATIONS and isinstance(data.get("data"), dict):
                catalog._visit(data["data"], {}, "/data", rid, tool)
            if tool in EVIDENCE_TOOLS:
                catalog._visit(data, {}, "", rid, tool)
        unique = {}
        for fact in catalog.facts.values():
            identity = tuple(value for key, value in asdict(fact).items() if key not in {"id", "receipt_id", "pointer"})
            unique.setdefault(identity, fact)
        catalog.facts = {fact.id: fact for fact in unique.values()}
        return catalog

    def _ledger(self, data, rid):
        if data.get("available") is not True:
            return
        period = f"{data.get('from_date', '?')}至{data.get('through_date', '?')}"
        def add(pointer, value, label, unit="次", kind="count"):
            if kind == "count":
                try:
                    _number(value)
                except ValueError:
                    return
            fid = hashlib.sha256((rid + pointer).encode()).hexdigest()[:24]
            self.facts[fid] = Fact(fid, rid, pointer, period, "", pointer, label, str(value), unit,
                                  None, kind, "任务运行台账")
        add("/total_runs", data.get("total_runs"), "任务运行总数")
        states = {"completed": "已完成", "degraded": "部分完成", "failed": "失败", "skipped": "已跳过",
                  "running": "运行中", "queued": "等待中"}
        for status, count in data.get("status_counts", {}).items():
            if status in states:
                add(f"/status_counts/{status}", count, states[status])
        for trigger, count in data.get("by_trigger", {}).items():
            if isinstance(count, (int, float)):
                add(f"/by_trigger/{_pointer(trigger)}", count, f"触发方式{trigger}")
        for index, row in enumerate(data.get("recent_runs", [])):
            if isinstance(row, dict) and row.get("error"):
                add(f"/recent_runs/{index}/error", row["error"],
                    f"{row.get('job_id')}（{row.get('started_at')}）原始失败原因", "", "quotation")

    def _visit(self, value, inherited, path, rid, tool):
        if isinstance(value, list):
            for i, item in enumerate(value):
                self._visit(item, inherited, f"{path}/{i}", rid, tool)
            return
        if not isinstance(value, dict):
            return
        meta = value.get("_meta") if isinstance(value.get("_meta"), dict) else {}
        if (any(value.get(k) or meta.get(k) for k in ("is_sample", "is_synthetic", "synthetic", "error"))
                or any(isinstance(value.get(k), str) and value[k] in {"sample", "synthetic", "disabled"} for k in ("source", "provider", "data_status"))
                or meta.get("source") in {"sample", "synthetic"}
                or value.get("has_real_fundamentals") is False):
            return
        context = {**inherited}
        parts = path.split("/")
        compact_bar = (tool == "batch_get_bars" and len(parts) == 5
                       and parts[1] == "results" and parts[3] == "bars" and parts[4].isdigit())
        if compact_bar:
            # A row's date belongs to that bar; a missing date must not inherit
            # the batch's newest observation time.
            context.pop("observed_at", None)
        if tool in PORTFOLIO_OPERATIONS | {"analyze_portfolio"} and "/account_balances/" in path:
            context["account"] = value.get("id", context.get("account", ""))
        for key in ("symbol", "name", "account", "timeframe", "volume_unit", "price_unit", "url", "adjustment", "classification"):
            if isinstance(value.get(key), str):
                context[key] = value[key]
        if meta.get("volume_unit"):
            context["volume_unit"] = meta["volume_unit"]
        if meta.get("adjustment"):
            context["adjustment"] = meta["adjustment"]
        # The pulse timestamp describes generation, whereas index rows carry
        # their own observation date. A sector row must not inherit that date.
        time_fields = () if tool == "get_market_pulse" and not path else ("as_of", "closed_at", "timestamp", "date")
        stamp = next((value[k] for k in time_fields
                      if isinstance(value.get(k), str)), None)
        stamp = stamp or meta.get("data_as_of") or meta.get("as_of")
        if stamp == value.get("date") and value.get("time") and "T" not in str(stamp):
            stamp = f"{stamp}T{value['time']}"
        if stamp:
            context["observed_at"] = observation_time(stamp, value.get("timezone") or meta.get("timezone") or "Asia/Shanghai")
        # Retrieved_at is intentionally not an observation timestamp.
        for key, item in value.items():
            pointer = f"{path}/{_pointer(key)}"
            if key in _NON_OBSERVATIONS:
                continue
            metric = {"O": "open", "H": "high", "L": "low", "C": "close", "V": "volume"}.get(key, key) if compact_bar else key
            descriptor = FIELDS.get(metric)
            if path.endswith("/ma") and key.startswith("MA") and key[2:].isdigit():
                descriptor = (key, "元/股", "moving_average")
            if descriptor and isinstance(item, (str, int, float)) and not isinstance(item, bool):
                label, unit, kind = descriptor
                if kind not in {"identity", "quotation"}:
                    try:
                        _number(item)
                    except ValueError:
                        continue
                if kind == "volume":
                    unit = {"shares": "股", "lots": "手"}.get(context.get("volume_unit"), "单位未提供")
                if kind in {"price", "moving_average"} and (context.get("price_unit") == "index_points" or path.startswith("/indices/")):
                    unit = "点"
                if kind in {"price", "cost", "entry", "exit"} and _number(item) <= 0:
                    continue
                if tool in PORTFOLIO_OPERATIONS | {"analyze_portfolio"}:
                    if "/recent_trades/" in path:
                        label = {"price": "历史成交价", "quantity": "历史成交数量"}.get(key, label)
                    elif "/recent_closed_trades/" in path and key == "quantity":
                        label = "FIFO结算分配数量"
                    if kind == "settlement" and value.get("pnl_basis") != "fifo_after_entry_and_exit_fees":
                        continue
                if key == "count":
                    if tool == "get_bars":
                        label = "取回K线数量"
                    elif tool == "get_market_pulse" and path == "/limit_up":
                        label = "涨停数量"
                if kind == "identity" and not str(item).strip():
                    continue
                if key in {"last_price", "market_value", "unrealized_pnl", "pnl_pct"} and value.get("price_is_fresh") is False:
                    continue
                fid = hashlib.sha256((rid + pointer).encode()).hexdigest()[:24]
                subject = " ".join(context.get(k, "") for k in ("symbol", "name")).strip()
                scope = {k: context[k] for k in ("timeframe", "adjustment", "classification", "url") if context.get(k)}
                if tool == "get_fund_flow":
                    category = path.split("/")[1] if path else ""
                    if category in {"industry_flow", "concept_flow"}:
                        scope["category"] = {"industry_flow": "行业", "concept_flow": "概念"}[category]
                for k in ("adjusted", "high_low_basis", "volume_basis", "pnl_basis", "price_type", "market_date", "trade_id", "entry_trade_id", "exit_trade_id"):
                    if k in value:
                        scope[k] = value[k]
                if key in {"high", "low"} and value.get("high_low_basis") == "recent_5_bars":
                    label = "最近五根K线" + label
                # Quotations retain their exact content and origin; they are not
                # parsed into numeric observations by the report composer.
                self.facts[fid] = Fact(fid, rid, pointer, subject, context.get("account", ""),
                    metric, label, str(item), unit, context.get("observed_at"), kind, tool,
                    context.get("symbol") or context.get("name", ""), context.get("timeframe", ""),
                    json.dumps(scope, ensure_ascii=False) if scope else "")
            if isinstance(item, (dict, list)):
                child_context = context
                if path == "/results" and tool in {"batch_get_bars", "batch_get_fundamentals", "batch_search_news"}:
                    child_context = {**context, "symbol": key}
                self._visit(item, child_context, pointer, rid, tool)

    def public_facts(self):
        # The field pointer/receipt hash remain in the owned artifact. Repeating
        # them (and the entity identity) in every model row can exceed the loaded
        # model's input budget even for one ordinary day's report.
        return [{key: value for key, value in asdict(fact).items()
                 if key not in {"receipt_id", "pointer", "entity", "timeframe"} and value not in ("", None)}
                for fact in self.facts.values()]

    def model_table(self):
        columns = ["id", "subject", "account", "metric", "label", "value", "unit", "observed_at", "kind", "source", "scope"]
        return {"fact_columns": columns, "facts": [[getattr(fact, column) for column in columns] for fact in self.facts.values()]}

    def resolve(self, ref):
        if ref not in self.facts:
            raise ValueError("引用不属于本轮可核验资料")
        return self.facts[ref]


def calculate(item: Calculation, catalog: EvidenceCatalog) -> tuple[str, dict]:
    facts = [catalog.resolve(ref) for ref in item.refs]
    if len(set(item.refs)) != len(item.refs):
        raise ValueError("计算重复引用了同一数据")
    if len({f.unit for f in facts}) != 1 or any(f.unit == "单位未提供" for f in facts):
        raise ValueError("计算单位不一致或未知")
    if any(f.kind in {"identity", "quotation"} for f in facts):
        raise ValueError("计算引用必须为数值")
    values = [_number(f.value) for f in facts]
    if item.operation != "sum" and len(values) != 2:
        raise ValueError("该计算需要两个操作数")
    if item.operation in {"difference", "change_pct", "compare"}:
        if not (facts[0].entity or facts[0].account) or len({(f.entity, f.account, f.timeframe, f.scope) for f in facts}) != 1:
            raise ValueError("不能混用不同标的或账户的数值")
        price_kinds = {"price", "cost", "entry", "exit", "moving_average"}
        if len({f.metric for f in facts}) != 1 and not all(f.kind in price_kinds for f in facts):
            raise ValueError("不能混用不同含义的指标")
    if item.operation in {"compare", "difference", "share_pct"}:
        if len({f.observed_at for f in facts}) != 1 or (not facts[0].observed_at and len({f.receipt_id for f in facts}) != 1):
            raise ValueError("无法确认比较数值属于同一观察时点")
    if item.operation == "sum":
        if len({(f.metric, f.observed_at, f.scope) for f in facts}) != 1 or not all(f.entity or f.account for f in facts):
            raise ValueError("合计需要同一指标和观察时点")
        if not facts[0].observed_at and len({f.receipt_id for f in facts}) != 1:
            raise ValueError("跨来源合计缺少共同观察时点")
        if len({(f.entity, f.account) for f in facts}) != len(facts):
            raise ValueError("合计重复包含同一标的或账户")
        result, label, unit = sum(values), "合计", facts[0].unit
    elif item.operation == "difference":
        result, label, unit = values[1] - values[0], "后项减前项", facts[0].unit
    elif item.operation in {"change_pct", "share_pct"}:
        denominator = values[0] if item.operation == "change_pct" else values[1]
        if denominator <= 0:
            raise ValueError("比例计算需要明确的正数分母")
        if item.operation == "share_pct":
            same_account = (facts[0].account and facts[0].account == facts[1].account
                            and facts[0].metric == "market_value" and facts[1].metric in {"capital", "account_capital"})
            whole_portfolio = (facts[0].metric == "total_market_value" and facts[1].metric == "total_account_capital"
                               and facts[0].receipt_id == facts[1].receipt_id)
            if not (same_account or whole_portfolio) or facts[0].observed_at != facts[1].observed_at:
                raise ValueError("占比计算需要同一账户和观察时点")
            result, label = values[0] / denominator * 100, "前项占后项比例"
        else:
            # A price change is not a fee-inclusive settlement return.
            if not all(f.kind in {"price", "cost", "entry", "exit"} for f in facts):
                raise ValueError("价格变动比例必须引用同一标的价格，不能替代净结算收益")
            if not (facts[0].kind == "entry" and facts[1].kind == "exit" and facts[0].receipt_id == facts[1].receipt_id):
                if not all(f.observed_at for f in facts) or facts[0].observed_at >= facts[1].observed_at:
                    raise ValueError("价格变动需要明确且先后有序的观察日期")
            result, label = (values[1] - values[0]) / denominator * 100, "价格变动比例（不含费用）"
        unit = "%"
    else:
        sign = "<" if values[0] < values[1] else ">" if values[0] > values[1] else "="
        return f"{facts[0].display()} {sign} {facts[1].display()}", {"result": sign, "refs": item.refs}
    rendered = format(result.quantize(Decimal("0.01")), "f")
    operands = "；".join(f.display() for f in facts)
    return f"{label}：{rendered}{unit}。依据：{operands}", {
        "operation": item.operation, "refs": item.refs, "result": str(result), "unit": unit}


def inference_evidence(item: Inference, document: ReportDocument, catalog: EvidenceCatalog):
    """Resolve judgments to source observations and verified calculations.

    Calculations keep their raw-fact-only operand contract. Revalidating them
    here makes resolution independent of document order and rejects cycles,
    invalid operands and scope conflicts before semantic review.
    """
    items = {entry.id: entry for entry in document.items}
    facts, calculations = {}, {}
    for ref in item.refs:
        if ref in catalog.facts:
            if ref in items:
                raise ValueError("报告条目ID与事实ID冲突，引用含义不明确")
            fact = catalog.resolve(ref)
            facts[fact.id] = fact
        elif isinstance(items.get(ref), Observation):
            # An observation is only a direct alias for one owned fact. Do not
            # recurse through report items or accept prose as source evidence.
            fact = catalog.resolve(items[ref].ref)
            facts[fact.id] = fact
        elif isinstance(items.get(ref), Calculation):
            calculation = items[ref]
            _, trace = calculate(calculation, catalog)
            calculations[ref] = {"id": ref, "operation": calculation.operation, **trace}
            for operand in calculation.refs:
                fact = catalog.resolve(operand)
                facts[fact.id] = fact
        else:
            raise ValueError("研判引用不属于本轮事实或已验证计算")
    return list(facts.values()), list(calculations.values())


def validate_document(document: ReportDocument, catalog: EvidenceCatalog):
    findings, rendered, traces = [], {}, {}
    ids = [item.id for item in document.items]
    if len(ids) != len(set(ids)):
        return [Finding("report", "duplicate_item", "报告条目重复，未完成核验")], {}, {}
    for item in document.items:
        try:
            if item.id in catalog.facts:
                raise ValueError("报告条目ID与事实ID冲突，引用含义不明确")
            if isinstance(item, Observation):
                rendered[item.id] = catalog.resolve(item.ref).display()
            elif isinstance(item, Calculation):
                rendered[item.id], traces[item.id] = calculate(item, catalog)
            elif isinstance(item, Condition):
                fact = catalog.resolve(item.ref)
                if fact.unit != item.unit or fact.kind not in {"price", "moving_average", "indicator", "ratio", "percentage"}:
                    raise ValueError("计划阈值与参考指标的单位或类型不一致")
                threshold = _number(item.threshold)
                op = {"lt": "低于", "le": "不高于", "gt": "高于", "ge": "不低于"}[item.comparison]
                action = {"review": "重新评估", "consider_buy": "评估买入", "consider_sell": "评估卖出", "wait": "继续观察"}[item.action]
                if item.quantity is not None:
                    if item.action not in {"consider_buy", "consider_sell"}:
                        raise ValueError("非买卖计划不能指定数量")
                    action += f"{item.quantity}股"
                rendered[item.id] = (f"条件计划：若{fact.subject}的{fact.label}{op}{threshold}{item.unit}，{action}。"
                                     f"该条件不是已发生事实或已执行指令。参考：{fact.display()}")
            elif isinstance(item, Inference):
                inference_evidence(item, document, catalog)
            else:
                findings.append(Finding("data", "declared_gap", item.text, item.id))
                rendered[item.id] = "待核实：" + item.text
        except (ValueError, InvalidOperation) as exc:
            findings.append(Finding("report", "invalid_evidence", str(exc), item.id))
    return findings, rendered, traces


def render_document(document, rendered, findings):
    lines, current = [], None
    by_item = {finding.item_id: finding for finding in findings if finding.item_id}
    for item in document.items:
        if item.section != current:
            current = item.section
            lines.append(f"\n## {SECTION_LABELS[current]}\n")
        if item.id in rendered:
            lines.append(rendered[item.id] + "\n")
        elif item.id in by_item:
            lines.append("暂未形成可核实结论：" + by_item[item.id].message + "\n")
    return "\n".join(lines).strip()


def verification_result(findings, *, report="complete", execution="completed", data="available"):
    rows = [asdict(f) if isinstance(f, Finding) else f for f in findings]
    rows = [{**row, "sources": list(row.get("sources", []))} for row in rows]
    return {"schema_version": 1, "report": report, "execution": execution, "data": data,
            "status": "complete" if not rows and report == "complete" and execution == "completed" and data == "available" else "incomplete",
            "findings": rows}


def incomplete(result):
    return bool(result) and result.get("status") != "complete"


def combine_verifications(steps):
    """Aggregate typed outcomes without interpreting exception message text."""
    findings, children = [], {}
    report, execution, data = "complete", "completed", "available"
    for step, output in steps.items():
        payload = output.get("data") if isinstance(output.get("data"), dict) else output
        verified = payload.get("verification") or output.get("verification")
        if not isinstance(verified, dict) or not verified:
            if output.get("error") or payload.get("error"):
                verified = verification_result([Finding("execution", "step_failed", str(output.get("error") or payload["error"]))], execution="failed")
            elif output.get("degraded") or payload.get("degraded"):
                verified = verification_result([Finding("workflow", "step_incomplete", "该步骤未完整完成")], report="partial")
            else:
                continue
        children[step] = verified
        findings.extend({**row, "step_id": step} for row in verified.get("findings", []))
        if verified.get("report") != "complete":
            report = "partial"
        if verified.get("execution") != "completed":
            execution = "incomplete"
        if verified.get("data") != "available":
            data = "insufficient"
    if not children:
        return {}
    return {**verification_result(findings, report=report, execution=execution, data=data), "steps": children}
