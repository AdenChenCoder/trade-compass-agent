"""Lightweight QA verification for agent outputs.

Two-layer design:
1. verify_claims: deterministic numeric cross-check (high-precision extraction)
2. qa_review: single LLM call for semantic/logical verification (uses verify_claims as context)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


def unverified_execution_claim(text: str, tool_results: list[tuple[str, str]]) -> bool:
    """Reject affirmative current-trade claims without an execution receipt.

    Advice, rejected orders, and queried historical trades are not executions.
    This is a high-precision check; it does not infer orders from natural language.
    """
    plain = re.sub(r"[*`#]", "", text)
    # Remove only an attributed quote immediately and explicitly withdrawn.
    # Keep surrounding current claims: negation in a later clause cannot hide them.
    plain = re.sub(
        r"(?:此前|旧|原|历史)(?:记忆|记录|报告|日志|摘要|文)(?:中|里)?\s*[「“『][^」”』\n]*[」”』]"
        r"(?=\s*(?:与[^。\n；;]{0,30}不符|(?:为|是|属于)?(?:错误|有误|不实|作废|未证实)))",
        "", plain,
    )
    claims = []
    for line in re.split(r"[。；;，,\n]", plain):
        # An empty status heading is not a fill. Inspect any following text normally.
        line = re.sub(r"^\s*[-•]?\s*(?:(?:本轮|本次)\s*)?已成交(?:[（(](?:本轮|本次)[）)])?"
                      r"\s*[:：|]\s*(?:无|0\s*笔?)(?=$|[\s（(])", "", line.strip().strip("|"))
        if re.search(r"未|没有|不曾|失败|拒绝|建议|计划|方案|如果|假设|例如|示例", line):
            continue
        if re.search(r"已(?:经)?(?:成功)?(?:执行)?(?:模拟|自动)?(?:买入|卖出|下单|成交)|"
                     r"已完成[^。\n]{0,30}(?:买入|卖出)[^。\n]{0,12}(?:\d+|一百)\s*股|"
                     r"成交成功|交易已记录|交易状态[^。\n]{0,25}executed|"
                     r"执行成功[^。\n]{0,45}(?:买入|卖出|下单|成交|\d+\s*股)", line, re.IGNORECASE):
            claims.append(line)
    if not claims:
        return False
    ledger_facts = False
    fills = []
    for name, raw in tool_results:
        if name not in {"place_paper_trade", "batch_paper_trades", "analyze_portfolio"}:
            continue
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or payload.get("error"):
            continue
        rows = payload.get("results", []) if name == "batch_paper_trades" else [payload]
        if name == "analyze_portfolio":
            ledger_facts = ledger_facts or any(isinstance(row, dict) and row.get("trade_id")
                                             for row in payload.get("recent_trades", []))
        else:
            fills.extend(row for row in rows if isinstance(row, dict)
                         and row.get("status") == "executed" and row.get("trade_id"))
    if fills:
        fills = list({str(row["trade_id"]): row for row in fills}.values())
        for claim in claims:
            symbols = set(re.findall(r"(?<!\d)\d{6}(?!\d)(?!\s*股)", claim))
            scoped = [r for r in fills if not symbols or r.get("symbol") in symbols]
            if symbols and all(r.get("symbol") for r in fills) and not scoped:
                return True
            quantities = re.findall(r"(\d+)\s*股", claim)
            if len(scoped) == 1 and quantities and scoped[0].get("quantity") is not None:
                if any(int(q) != scoped[0]["quantity"] for q in quantities):
                    return True
            ids = re.findall(r"(?:trade_id|成交ID|成交 ID)\s*[:：=]?\s*[`\"]?([\w-]+)", claim)
            if ids and any(identity not in {str(r["trade_id"]) for r in fills} for identity in ids):
                return True
            for word, side in (("买入", "buy"), ("卖出", "sell")):
                if word in claim and scoped and all(r.get("side") for r in scoped):
                    if not any(r["side"] == side for r in scoped):
                        return True
        return False
    return not ledger_facts or any(re.search(r"本轮|本次|刚刚", line) for line in claims)


def trade_receipt_section(tool_results: list[tuple[str, str]]) -> str:
    """Render today's queried ledger facts and current executions without an LLM.

    A ledger query is not an order, and FIFO settlements are not executions.
    Repeated portfolio queries must not turn one execution into several fills.
    """
    facts: dict[str, dict] = {}
    executed = set()
    for name, raw in tool_results:
        if name not in {"analyze_portfolio", "place_paper_trade", "batch_paper_trades"}:
            continue
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or payload.get("error"):
            continue
        if name == "analyze_portfolio":
            day = str(payload.get("trades_as_of") or "")[:10]
            rows = [r for r in payload.get("recent_trades", []) if isinstance(r, dict)
                    and day and str(r.get("timestamp", ""))[:10] == day]
        else:
            rows = [payload] if name == "place_paper_trade" else payload.get("results", [])
            rows = [r for r in rows if isinstance(r, dict) and r.get("status") == "executed"]
        for row in rows:
            identity = row.get("trade_id")
            if not identity or row.get("side") not in {"buy", "sell"}:
                continue
            if not all(row.get(k) is not None for k in ("symbol", "quantity", "price", "account")):
                continue
            facts[str(identity)] = row
            if name != "analyze_portfolio":
                executed.add(str(identity))
    if not facts:
        return ""

    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = ["\n\n**成交核对**", "", "以下逐笔数据来自本轮成交回执或当日账本查询；账本记录不表示本轮新下单。",
             "", "| 依据 | 时间 | 账户 | 标的 | 方向 | 数量 | 价格 | 成交 ID |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for identity, row in sorted(facts.items(), key=lambda pair: str(pair[1].get("timestamp", ""))):
        values = ["本轮回执" if identity in executed else "当日账本", row.get("timestamp") or "未提供",
                  row["account"], row["symbol"], "买入" if row["side"] == "buy" else "卖出",
                  row["quantity"], row["price"], identity]
        lines.append("| " + " | ".join(cell(v) for v in values) + " |")
    return "\n".join(lines)


_PRICE_PATTERN = re.compile(
    r"(?:当前价|收盘价?|现价|最新价|开盘价?)[^\d]{0,5}(\d+\.?\d*)"
)
_RSI_PATTERN = re.compile(r"RSI[(\d)]*[^\d]{0,5}(\d+\.?\d*)")
_MA_PATTERN = re.compile(r"MA(\d+)[^\d]{0,5}(\d+\.?\d*)")
_PE_PATTERN = re.compile(r"(?:市盈率|PE)[^\d]{0,5}(\d+\.?\d*)")
_SKIP_CONTEXTS = re.compile(r"(目标|预测|可能达到|去年|历史|如果|假设)")

_SYMBOL_PATTERN = re.compile(r"\b(\d{6})\b")


@dataclass
class NumericClaim:
    text: str
    kind: str
    value: float
    context_start: int


@dataclass
class Violation:
    claim: NumericClaim
    actual: float
    deviation_pct: float


@dataclass
class VerifyResult:
    ok: bool
    claims_checked: int
    violations: list[Violation] = field(default_factory=list)


def verify_claims(response_text: str, tool_results: list[tuple[str, str]]) -> VerifyResult:
    """Extract numeric claims and cross-reference against tool output.

    Returns raw findings. False positives are possible — designed for use
    as context input to a QA LLM, not as a hard gate.
    """
    claims = _extract_claims(response_text)
    if not claims:
        return VerifyResult(ok=True, claims_checked=0)

    ground_truth = _build_ground_truth(tool_results)
    violations: list[Violation] = []

    for claim in claims:
        actual = ground_truth.get(claim.kind)
        if actual is None:
            continue
        if actual == 0:
            continue
        deviation = abs(claim.value - actual) / abs(actual)
        if deviation > 0.05:
            violations.append(Violation(claim=claim, actual=actual, deviation_pct=round(deviation * 100, 1)))

    return VerifyResult(
        ok=len(violations) == 0,
        claims_checked=len(claims),
        violations=violations,
    )


def format_verify_result(result: VerifyResult) -> str:
    """Format verification result as JSON for QA agent context."""
    return json.dumps({
        "claims_checked": result.claims_checked,
        "violations_found": len(result.violations),
        "violations": [
            {
                "claimed": f"{v.claim.kind}={v.claim.value}",
                "actual": v.actual,
                "deviation_pct": v.deviation_pct,
                "text_fragment": v.claim.text[:60],
            }
            for v in result.violations
        ],
    }, ensure_ascii=False)


QA_SYSTEM_PROMPT = """\
你是交易分析质检员。你的任务是检查 Agent 回复的数据准确性和逻辑合理性。

你会收到：
1. Agent 本轮调用的工具及其返回数据
2. Agent 最终回复文本
3. verify_claims 工具的自动数值校验结果

请检查：
1. verify_claims 发现的数值偏差是否为真正的错误（排除历史引用、预测目标等合理偏差）
2. 回复中的分析结论是否能从工具数据合理推导
3. 是否存在工具数据未覆盖但回复中出现的"事实性声明"
4. 方向性建议的信心水平是否与数据充分度匹配

仅输出 JSON：
{"pass": true/false, "issues": ["问题描述..."], "severity": "none|minor|critical"}

severity 定义：
- none: 无问题
- minor: 小偏差，不影响决策（如四舍五入）
- critical: 可能导致错误决策的重大偏差
"""


def build_qa_messages(
    response_text: str,
    tool_results: list[tuple[str, str]],
    verify_result: VerifyResult,
) -> list[dict[str, str]]:
    """Build messages for a single QA LLM call."""
    tool_summary_parts = []
    for tool_name, result in tool_results[-10:]:
        try:
            payload = json.loads(result)
            if isinstance(payload, dict):
                payload.pop("bars", None)
                payload.pop("_meta", None)
            tool_summary_parts.append(f"[{tool_name}]: {json.dumps(payload, ensure_ascii=False)[:500]}")
        except (json.JSONDecodeError, TypeError):
            tool_summary_parts.append(f"[{tool_name}]: {result[:200]}")

    user_content = (
        f"## 工具数据摘要\n{chr(10).join(tool_summary_parts)}\n\n"
        f"## 数值校验结果\n{format_verify_result(verify_result)}\n\n"
        f"## Agent 回复\n{response_text[:2000]}"
    )

    return [
        {"role": "system", "content": QA_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _extract_claims(text: str) -> list[NumericClaim]:
    claims: list[NumericClaim] = []

    for match in _PRICE_PATTERN.finditer(text):
        start = max(0, match.start() - 10)
        context = text[start:match.start()]
        if _SKIP_CONTEXTS.search(context):
            continue
        try:
            claims.append(NumericClaim(
                text=text[max(0, match.start() - 5):match.end() + 5],
                kind="close",
                value=float(match.group(1)),
                context_start=match.start(),
            ))
        except ValueError:
            pass

    for match in _RSI_PATTERN.finditer(text):
        start = max(0, match.start() - 10)
        context = text[start:match.start()]
        if _SKIP_CONTEXTS.search(context):
            continue
        try:
            claims.append(NumericClaim(
                text=text[max(0, match.start() - 5):match.end() + 5],
                kind="rsi",
                value=float(match.group(1)),
                context_start=match.start(),
            ))
        except ValueError:
            pass

    for match in _PE_PATTERN.finditer(text):
        start = max(0, match.start() - 10)
        context = text[start:match.start()]
        if _SKIP_CONTEXTS.search(context):
            continue
        try:
            claims.append(NumericClaim(
                text=text[max(0, match.start() - 5):match.end() + 5],
                kind="pe",
                value=float(match.group(1)),
                context_start=match.start(),
            ))
        except ValueError:
            pass

    return claims


def _build_ground_truth(tool_results: list[tuple[str, str]]) -> dict[str, float]:
    """Extract latest ground truth values from tool outputs."""
    truth: dict[str, float] = {}

    for tool_name, result in tool_results:
        try:
            payload = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue

        if tool_name == "get_bars":
            bars = payload.get("bars")
            if bars and isinstance(bars, list):
                last_bar = bars[-1]
                close = last_bar.get("close")
                if close is not None:
                    truth["close"] = float(close)

        elif tool_name == "compute_rsi":
            rsi = payload.get("current_rsi")
            if rsi is not None:
                truth["rsi"] = float(rsi)

        elif tool_name == "get_fundamentals":
            pe = payload.get("pe_ttm")
            if pe is not None:
                truth["pe"] = float(pe)

    return truth
