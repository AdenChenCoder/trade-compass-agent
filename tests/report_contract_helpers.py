"""Explicit model doubles for the versioned report wire protocol."""
import json

from trade_compass_agent.llm.providers import ChatCompletion


def report_facts(payload):
    dictionaries = payload.get("fact_dictionaries", {})
    result = []
    for group in payload["facts"]:
        identity = {key: value for key, value in group.items() if key != "rows"} if isinstance(group, dict) else {}
        rows = group["rows"] if isinstance(group, dict) else [group]
        result.extend({**identity, **{column: dictionaries[column][value] if column in dictionaries else value
                       for column, value in zip(payload["fact_columns"], row, strict=True)}} for row in rows)
    if "fact_order" in payload:
        by_id = {row["id"]: row for row in result}
        return [by_id[ref] for ref in payload["fact_order"]]
    return result


def report_reply(messages, *, metrics=None, gap=None, inference=None, invalid_ref=False):
    """Return only publication replies; normal agent rounds remain test-owned."""
    try:
        payload = json.loads(messages[-1].content)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if "schema" in payload and "facts" in payload:
        facts = [f for f in report_facts(payload) if metrics is None or f["metric"] in metrics]
        items = [{"id": f"fact-{i}", "type": "fact", "section": "positions", "ref": f["id"]}
                 for i, f in enumerate(facts[:80])]
        if invalid_ref and items:
            items[0]["ref"] = "not-an-owned-fact"
        if gap:
            items.append({"id": "gap", "type": "gap", "section": "gaps", "text": gap})
        if inference and facts:
            items.append({"id": "judgment", "type": "inference", "section": "assessment", "text": inference, "refs": [facts[0]["id"]]})
        return ChatCompletion(content=json.dumps({"schema_version": 1, "items": items}))
    if "inferences" in payload and "report" in payload:
        return ChatCompletion(content=json.dumps({"reviews": [{"id": item["id"], "supported": True, "reason": ""}
            for item in payload["inferences"]], "coverage": {"complete": True, "missing": []}}))
    return None
