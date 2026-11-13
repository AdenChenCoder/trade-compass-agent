"""Reader presentation of already validated report items.

No model call, new claim, precision change or state mutation occurs here.
Review and publication use the same presentation. Narrative paragraphs retain
their author-chosen order; legacy documents keep their readable fact layout.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from trade_compass_agent.runtime.report_contract import (
    Calculation, Condition, Observation, Paragraph, Inference, SECTION_LABELS,
    inference_evidence,
)

DISPLAY_TIMEZONE = "Asia/Shanghai"


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
            stamp = stamp.astimezone(ZoneInfo(DISPLAY_TIMEZONE))
            if fact.timeframe == "1d":
                return stamp.strftime("%Y-%m-%d")
            return stamp.strftime("%Y-%m-%d %H:%M:%S") + "（北京时间）"
    except ValueError:
        pass
    return fact.observed_at


def render_readable_document(document, rendered, findings, catalog, *, execution=None, include_sources=True):
    """Preserve section/item order, merging recurring sections and fact blocks.

    Group only observations already present in ``rendered``. Failed items never
    acquire a rendered value by resolving their references in this layer.
    """
    if any(isinstance(item, Paragraph) for item in document.items):
        return _narrative(document, rendered, findings, catalog, execution=execution, include_sources=include_sources)
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
    if notes and include_sources:
        lines.append("## 数据来源与口径")
        lines.extend(f"- 〔{number}〕{_plain(context)}" for context, number in notes.items())
    return "\n\n".join(lines)


def _narrative(document, rendered, findings, catalog, *, execution, include_sources=True):
    """Display approved prose, with independently navigable source footnotes.

    Fact/calculation items are evidence definitions in this format. They are
    not inserted between the writer's sentences. If no paragraph survives,
    retain validated facts as a partial report, never as a successful answer.
    """
    blocks, notes = [], {}
    approved = any(isinstance(item, Paragraph) and rendered.get(item.id, "").strip() for item in document.items)
    unresolved = {finding.item_id for finding in findings if finding.item_id}
    incomplete_sections = set()

    def incomplete_notice(item):
        # Review diagnostics may quote rejected claims or internal field names.
        # Keep them in the artifact; the reader needs the unfinished business area.
        if item.section not in incomplete_sections:
            incomplete_sections.add(item.section)
            blocks.append(f"{SECTION_LABELS[item.section]}部分尚未完成，相关结论暂无法确认。")

    def partial_evidence(item):
        # Only program-rendered, validated definitions reach this fallback.
        # Move their source metadata to the same disclosure used by prose,
        # without parsing or rewriting any author-written sentence.
        refs = [item.ref] if isinstance(item, Observation) else item.refs
        facts = [catalog.resolve(ref) for ref in dict.fromkeys(refs)]
        text = rendered[item.id]
        for fact in facts:
            identity = " · ".join(x for x in (fact.subject, fact.account) if x)
            compact = (f"{_plain(identity) + '：' if identity else ''}{_plain(fact.label)} "
                       f"{_plain(fact.value)}{_plain(fact.unit)}（{_plain(_date(fact))}）")
            text = text.replace(fact.display(), compact)
        sources = tuple(fact.display() for fact in facts)
        notes.setdefault(sources, len(notes) + 1)
        return text + f"[^source-{notes[sources]}]"
    for item in document.items:
        if isinstance(item, (Observation, Calculation)):
            if item.id in unresolved:
                incomplete_notice(item)
            if not approved and item.id in rendered:
                blocks.append(partial_evidence(item))
            continue
        if item.id not in rendered:
            if item.id in unresolved:
                incomplete_notice(item)
            continue
        text = rendered[item.id]
        if not text.strip():
            continue
        if isinstance(item, (Paragraph, Inference)):
            facts, calculations = inference_evidence(item, document, catalog)
            sources = [fact.display() for fact in facts]
            for calculation in calculations:
                entry = next(entry for entry in document.items if entry.id == calculation["id"])
                if entry.id in rendered:
                    sources.append(rendered[entry.id])
            if sources:
                key = tuple(sources)
                notes.setdefault(key, len(notes) + 1)
                text += f"[^source-{notes[key]}]"
        blocks.append(text)
    if execution:
        blocks.append(execution.render(include_table=False).strip())
        blocks.append(execution.trade_details.strip())
    if include_sources:
        for sources, number in notes.items():
            blocks.append(f"[^source-{number}]: " + "；".join(_plain(source) for source in sources))
    return "\n\n".join(block for block in blocks if block)
