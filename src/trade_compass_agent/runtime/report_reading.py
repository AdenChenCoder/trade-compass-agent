"""Reader presentation of already validated report items.

No model call, new claim, precision change or state mutation occurs here. The
reviewer keeps the full audit presentation; readers get grouped observations
and reusable source notes with the same values and exact context.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from trade_compass_agent.runtime.report_contract import (
    Calculation, Condition, Observation, SECTION_LABELS,
)


def _plain(value: str) -> str:
    """Keep receipt text literal in Markdown headings and table cells."""
    value = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for char in "\\`*_{}[]|#":
        value = value.replace(char, "\\" + char)
    return value.replace("\r", " ").replace("\n", " ")


def _date(fact) -> str:
    if not fact.observed_at:
        return "时点未提供"
    try:
        stamp = datetime.fromisoformat(fact.observed_at)
        if stamp.tzinfo:
            stamp = stamp.astimezone(ZoneInfo("Asia/Shanghai"))
            if fact.timeframe == "1d":
                return stamp.strftime("%Y-%m-%d")
            return stamp.strftime("%Y-%m-%d %H:%M:%S") + "（北京时间）"
    except ValueError:
        pass
    return fact.observed_at


def render_readable_document(document, rendered, findings, catalog, *, execution=None):
    """Preserve section/item order, merging recurring sections and fact blocks.

    Group only observations already present in ``rendered``. Failed items never
    acquire a rendered value by resolving their references in this layer.
    """
    sections = defaultdict(list)
    for item in document.items:
        sections[item.section].append(item)
    if execution and execution.records:
        sections.setdefault("execution", [])
    notes = {}
    by_item = defaultdict(list)
    for finding in findings:
        if finding.item_id and finding.message not in by_item[finding.item_id]:
            by_item[finding.item_id].append(finding.message)

    def reference(fact):
        context = fact.context()
        if context not in notes:
            notes[context] = len(notes) + 1
        return f"〔{notes[context]}〕"

    def compact(fact):
        identity = " · ".join(x for x in (fact.subject, fact.account) if x)
        return (f"{_plain(identity) + '：' if identity else ''}{_plain(fact.label)} "
                f"{_plain(fact.value)}{_plain(fact.unit)}（{_plain(_date(fact))}）{reference(fact)}")

    def observations(items):
        groups = defaultdict(list)
        for item in items:
            fact = catalog.resolve(item.ref)
            groups[(fact.subject, fact.account)].append(fact)
        blocks = []
        for (subject, account), facts in groups.items():
            identity = " · ".join(x for x in (subject, account) if x)
            if len(facts) == 1:
                fact = facts[0]
                blocks.append(f"- {'**' + _plain(identity) + '**：' if identity else ''}"
                              f"{_plain(fact.label)} {_plain(fact.value)}{_plain(fact.unit)}{reference(fact)}  \n"
                              f"  {_plain(_date(fact))}")
                continue
            if identity:
                blocks.append(f"### {_plain(identity)}")
            dates = {_date(fact) for fact in facts}
            if len(dates) == 1:
                blocks.append(_plain(next(iter(dates))))
            rows = ["| 指标 | 数值 |", "| --- | --- |"]
            for fact in facts:
                label = _plain(fact.label)
                if len(dates) != 1:
                    label += f" · {_plain(_date(fact))}"
                rows.append(f"| {label}{reference(fact)} | {_plain(fact.value)}{_plain(fact.unit)} |")
            blocks.append("\n".join(rows))
        return "\n\n".join(blocks)

    lines = []
    for section, items in sections.items():
        lines.append(f"## {SECTION_LABELS[section]}")
        pending = []
        for item in items:
            if isinstance(item, Observation) and item.id in rendered:
                pending.append(item)
                continue
            if pending:
                lines.append(observations(pending))
                pending = []
            if item.id in rendered:
                text = rendered[item.id]
                # Only replace program-owned fact expansions, never model prose.
                if isinstance(item, (Calculation, Condition)):
                    refs = item.refs if isinstance(item, Calculation) else [item.ref]
                    for ref in dict.fromkeys(refs):
                        fact = catalog.resolve(ref)
                        text = text.replace(fact.display(), compact(fact))
                lines.append(text)
            elif item.id in by_item:
                lines.append("暂未形成可核实结论：" + "；".join(by_item[item.id]))
        if pending:
            lines.append(observations(pending))
        if section == "execution" and execution:
            lines.append(execution.render(heading=False, include_table=False).strip())
    if execution and execution.trade_details:
        lines.append(execution.trade_details.strip())
    if notes:
        lines.append("## 数据来源与口径")
        lines.extend(f"- 〔{number}〕{_plain(context)}" for context, number in notes.items())
    return "\n\n".join(lines)
