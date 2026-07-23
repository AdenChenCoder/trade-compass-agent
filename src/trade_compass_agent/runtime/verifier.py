"""Lightweight QA verification for agent outputs.

Two-layer design:
1. verify_claims: deterministic numeric cross-check (high-precision extraction)
2. qa_review: single LLM call for semantic/logical verification (uses verify_claims as context)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


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
