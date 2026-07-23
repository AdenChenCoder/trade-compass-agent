"""Dream Diary + Procedural Extraction — Dreaming Phases 4 & 5.

Phase 4: Agent-driven procedural extraction from strong trading patterns.
Phase 5: Agent-driven dream diary — reflective learning journal.

Both use ScheduledAgentSession for multi-step reasoning with tool access.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from trade_compass_agent.memory.insights import TradingInsight
from trade_compass_agent.memory.patterns import TradingPattern
from trade_compass_agent.memory.promotion import PromotionCandidate, PromotionResult
from trade_compass_agent.memory.time_tree import TimeNode

if TYPE_CHECKING:
    from trade_compass_agent.config import AppConfig

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Phase 4: Procedural Extraction
# ------------------------------------------------------------------

@dataclass
class ProcedureCandidate:
    name: str
    trigger_condition: str
    steps: list[str]
    evidence_count: int
    win_rate: float | None
    confidence: float
    counterexamples: list[str]


PROCEDURE_EXTRACTION_PROMPT = """\
你现在从交易模式中提炼可复用的操作流程。

## 已发现的强模式
{strong_patterns}

## 你的任务
对每个强模式（strength >= 0.7）：
1. 用 session_search 找到相关的历史会话
2. 还原你在这些会话中的工具调用序列（先做了什么 → 再做了什么）
3. 用 search_decisions 查看关联的交易决策和结果
4. 总结成一个可复用的操作流程：
   - 触发条件（什么时候启动这个流程）
   - 步骤序列（按顺序）
   - 历史胜率和注意事项
5. 如果找到失败案例，说明失败原因

写入技能库前必须先查重并优先更新：
1. 先调用 skill_manage(action="list") 查看现有技能。
2. 如果主题、触发条件或工具序列与已有 skill 重叠，先 skill_manage(action="view") 查看全文。
3. 能增量修正已有 skill 时，必须 skill_manage(action="patch")；patch 比 create 优先。
4. 只有没有相近 skill，且 evidence_count >= 5 且 confidence >= 0.7，才 skill_manage(action="create")。
5. 其他不够成熟的流程，只在回复中描述即可，不需要写入工具。
"""


def run_procedure_extraction(
    config: "AppConfig",
    strong_patterns: list[TradingPattern],
) -> str:
    """Run procedural extraction via ScheduledAgentSession.

    Returns the Agent's response text (which may include skill_manage patch/create calls).
    """
    from trade_compass_agent.ops.agent_session import ScheduledAgentSession

    patterns_text = "\n".join(
        f"- **{p.theme}** (strength={p.strength:.2f}, days={p.days_seen}): {p.description}"
        for p in strong_patterns
    )
    prompt = PROCEDURE_EXTRACTION_PROMPT.format(strong_patterns=patterns_text)
    session = ScheduledAgentSession(config, job_id="dreaming-procedural", memory_actor="dreaming")
    return session.run(prompt, timeout=300)


# ------------------------------------------------------------------
# Phase 5: Dream Diary
# ------------------------------------------------------------------

DREAM_DIARY_PROMPT = """\
你现在进行一次"记忆整理"（Dreaming），基于今天的交易活动回顾你学到了什么。

## 今日 Dreaming 摘要
{dreaming_summary}

## 你的任务
1. 用 session_search 工具回顾今天的关键对话
2. 用 search_memory 工具检查是否有跨天的关联
3. 直接输出一段 100-200 字的第一人称"交易学习日记"，重点：
   - 今天学到的新认知（如有）
   - 发现的重复模式意味着什么
   - 晋升到长期记忆的内容为什么重要
   - 明天需要关注什么

注意：直接在回复中输出日记内容即可，系统会自动保存。不需要调用 write_knowledge。

风格：简洁、反思性、像一个老练交易员的日记。不用 markdown。
"""

WEEKLY_DIARY_PROMPT = """\
你现在进行一次周度"深度记忆整理"（Weekly Dreaming），回顾本周的交易学习。

## 本周 Dreaming 摘要
{dreaming_summary}

## 你的任务
1. 用 session_search 工具回顾本周的重要对话
2. 用 search_memory 工具检查是否有重要的长期趋势
3. 直接输出一段 200-400 字的第一人称"周度交易复盘"，重点：
   - 本周最重要的交易认知变化
   - 哪些模式在强化，哪些在消退
   - 提炼出的操作流程是否可靠
   - 下周需要验证或关注什么

注意：直接在回复中输出复盘内容即可，系统会自动保存。不需要调用 write_knowledge。

风格：深入、系统性、像一个投资经理的周报给自己看。不用 markdown。
"""


def _read_last_diary(memory_dir: Path) -> str:
    """Read the last diary entry from DREAM_DIARY.md (most recent ## YYYY-MM-DD block)."""
    diary_path = memory_dir / "DREAM_DIARY.md"
    if not diary_path.is_file():
        return ""
    try:
        text = diary_path.read_text(encoding="utf-8")
    except Exception:
        return ""
    import re
    blocks = re.split(r"(?m)^---\n## \d{4}-\d{2}-\d{2}", text)
    if len(blocks) < 2:
        return ""
    last = blocks[-1].strip()
    return last[:400] if last else ""


def build_dreaming_summary(
    day_node: TimeNode | None,
    patterns: list[TradingPattern],
    promoted: list[PromotionCandidate] | list[PromotionResult],
    insights: list[TradingInsight],
    procedures_text: str = "",
    memory_dir: Path | None = None,
) -> str:
    """Assemble the summary that feeds into the Dream Diary prompt."""
    parts: list[str] = []

    if memory_dir:
        last_diary = _read_last_diary(memory_dir)
        if last_diary:
            parts.append(f"### 上次反思\n{last_diary}")

    if day_node:
        parts.append(f"### 今日概要\n{day_node.summary[:500]}")

    if patterns:
        parts.append(f"### 发现的模式 ({len(patterns)})")
        for p in patterns[:5]:
            parts.append(f"- {p.theme} (strength={p.strength:.2f}): {p.description[:100]}")

    if promoted:
        parts.append(f"### 晋升到长期记忆 ({len(promoted)})")
        for c in promoted[:5]:
            if isinstance(c, PromotionResult):
                parts.append(f"- [{c.verdict}] {c.refined_text[:100]}")
            else:
                parts.append(f"- [score={c.score:.3f}] {c.observation.summary[:100]}")

    if insights:
        parts.append(f"### 主动洞察 ({len(insights)})")
        for i in insights[:5]:
            parts.append(f"- [{i.kind.value}] {i.title}: {i.body[:80]}")

    if procedures_text:
        parts.append(f"### Procedural 提取\n{procedures_text[:500]}")

    return "\n\n".join(parts) if parts else "今日无显著交易活动。"


def run_dream_diary(
    config: "AppConfig",
    dreaming_summary: str,
    *,
    weekly: bool = False,
) -> str:
    """Run Dream Diary via ScheduledAgentSession.

    Returns the Agent's diary entry text.
    """
    from trade_compass_agent.ops.agent_session import ScheduledAgentSession

    template = WEEKLY_DIARY_PROMPT if weekly else DREAM_DIARY_PROMPT
    prompt = template.format(dreaming_summary=dreaming_summary[:4000])
    job_id = "dreaming-weekly-diary" if weekly else "dreaming-diary"
    session = ScheduledAgentSession(config, job_id=job_id)
    return session.run(prompt, timeout=300)


def append_dream_diary(memory_dir: Path, entry: str) -> Path:
    """Append a diary entry to DREAM_DIARY.md."""
    diary_path = memory_dir / "DREAM_DIARY.md"
    today = datetime.now().strftime("%Y-%m-%d")
    header = f"\n---\n## {today}\n\n"
    content = header + entry.strip() + "\n"
    with open(diary_path, "a", encoding="utf-8") as f:
        f.write(content)
    logger.info("Appended dream diary entry for %s", today)
    return diary_path
