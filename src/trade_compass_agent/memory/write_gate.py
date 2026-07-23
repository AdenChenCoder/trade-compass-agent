"""SemanticWriteGate — quality gate for Semantic tier (KNOWLEDGE.md / USER.md) writes.

Admission rules:
- minimum importance and concept support thresholds
- low admission scores are dropped
- explicit exclusions define what must not be saved

Any write to Semantic tier must pass all gates. This prevents the accumulation
of garbage that plagued the old MEMORY.md system.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trade_compass_agent.memory.skill_store import SkillStore


EPHEMERAL_PATTERNS = [
    r"今[天日]|昨[天日]|刚才|刚刚",
    r"(?:现价|收盘价|开盘价)\s*\d",
    r"bug|报错|修复|异常|error|exception",
    r"(?:上午|下午|早盘|尾盘)\d{1,2}[::]\d{2}",
]

MIN_ENTRY_LENGTH = 10
JACCARD_THRESHOLD = 0.5


def jaccard_similarity(a: str, b: str) -> float:
    """Bigram-based Jaccard similarity, suitable for Chinese short text."""
    def bigrams(text: str) -> set[str]:
        text = re.sub(r"\s+", "", text)
        return {text[i : i + 2] for i in range(len(text) - 1)}

    sa, sb = bigrams(a), bigrams(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def contains_ephemeral_markers(text: str) -> bool:
    """Check if text contains time-sensitive/ephemeral content markers."""
    return any(re.search(p, text) for p in EPHEMERAL_PATTERNS)


def is_covered_by_skills(text: str, skill_store: "SkillStore | None") -> bool:
    """Check if the content is already covered by an active skill."""
    if skill_store is None:
        return False
    skills = skill_store.list_skills(include_stale=False)
    text_lower = text.lower()
    for skill in skills:
        if skill.description and jaccard_similarity(text_lower, skill.description.lower()) > 0.5:
            return True
        try:
            content = skill.path.read_text(encoding="utf-8")
        except OSError:
            content = ""
        for chunk in _skill_content_chunks(content):
            chunk_lower = chunk.lower()
            if text_lower in chunk_lower or jaccard_similarity(text_lower, chunk_lower) > 0.5:
                return True
    return False


def _skill_content_chunks(content: str) -> list[str]:
    """Extract comparable chunks from SKILL.md without requiring a markdown parser."""
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3 :]

    chunks: list[str] = []
    for block in re.split(r"\n\s*\n", content):
        cleaned_lines = []
        for line in block.splitlines():
            line = re.sub(r"^\s{0,3}(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s*)", "", line).strip()
            if line:
                cleaned_lines.append(line)
        chunk = " ".join(cleaned_lines).strip()
        if len(chunk) >= MIN_ENTRY_LENGTH:
            chunks.append(chunk)
    return chunks


TRANSIENT_MARKERS = [
    r"正在|即将|马上",
    r"临时|暂时|先这样",
    r"测试.*数据|mock|dummy",
    r"v\d+\.\d+.*发布|更新了|升级到",
    r"[Ss]kill\s*更新记录|skill.*(?:重写|扩展|修改|新增)",
]


def passes_durability(text: str) -> bool:
    """Heuristic: would this still be true/useful in 30 days?"""
    for pattern in TRANSIENT_MARKERS:
        if re.search(pattern, text):
            return False
    return True


RAW_TOOL_OUTPUT_PATTERNS = [
    re.compile(r"^\[[\w_]+\]\s+\w+=.+;\s+\w+="),
    re.compile(r"^\[[\w_]+\]\s+\{"),
    re.compile(r"provider=\w+;\s*answer="),
]


def is_raw_tool_output(text: str) -> bool:
    """Detect summaries that are raw tool result serializations, not insights."""
    return any(p.search(text) for p in RAW_TOOL_OUTPUT_PATTERNS)


def quality_check(
    text: str,
    skill_store: "SkillStore | None" = None,
) -> tuple[bool, str]:
    """Content quality gate for promotion — checks ephemerality, durability, skill coverage.

    Called at *promotion* time (not write time) to filter observations before
    they enter KNOWLEDGE.md. Admission happens during consolidation or promotion,
    not during initial capture.
    """
    text = text.strip()
    if is_raw_tool_output(text):
        return False, "原始工具输出序列化，非提炼洞察"
    if contains_ephemeral_markers(text):
        return False, "包含时效性内容（日期/价格/时间），不适合 promotion"
    if not passes_durability(text):
        return False, "非持久性信息（一次性事件/临时状态）"
    if is_covered_by_skills(text, skill_store):
        return False, "已被现有 skill 覆盖"
    return True, "quality ok"


class SemanticWriteGate:
    """Dedup gate for Semantic tier (USER.md / KNOWLEDGE.md) writes.

    After the Phase 3 refactor, this gate only handles:
    1. Minimum length check
    2. Jaccard dedup against existing entries

    Quality checks (ephemeral, durability, skill coverage) have been moved
    to the ``quality_check()`` function, called at promotion time instead
    of write time — matching the consensus of all four reference projects.
    """

    def __init__(self, skill_store: "SkillStore | None" = None) -> None:
        self._skill_store = skill_store

    @property
    def skill_store(self) -> "SkillStore | None":
        return self._skill_store

    def should_admit(
        self,
        text: str,
        target: str,
        existing_entries: list[str],
    ) -> tuple[bool, str]:
        """Check if text should be admitted (dedup only).

        Returns (admitted: bool, reason: str).
        """
        text = text.strip()

        # Gate 1: Minimum length
        if len(text) < MIN_ENTRY_LENGTH:
            return False, "内容过短（<10字符）"

        # Gate 2: Semantic dedup — Jaccard bigram > threshold → reject
        for entry in existing_entries:
            sim = jaccard_similarity(text, entry)
            if sim > JACCARD_THRESHOLD:
                return False, f"与现有条目语义重复 (相似度 {sim:.2f}): '{entry[:30]}...'"

        return True, "admitted"
