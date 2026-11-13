"""Classify reader assertions without source evidence or prose truth heuristics."""
from __future__ import annotations


BODY_MEANING = """Read ONLY the supplied reader paragraphs. You have no execution evidence and must not decide truth or justify a claim.
Classify the exact meaning actually asserted about completed trading or memory-writing outcomes. Affirmative and negative outcomes both count. Do not soften a categorical outcome into uncertainty, or turn missing receipts into a negative outcome. Proposals, conditions, historical events, and inability to confirm an outcome are different meanings.
Partition EACH input text into contiguous parts at assertion boundaries. Copy text exactly, including punctuation/spacing; concatenating parts must reproduce the input byte-for-byte. Do not rewrite, normalize, add qualifiers, or omit any text. Split independent assertions when possible; retain sufficient original context to classify their meaning.
Return JSON: {"paragraphs":[{"id":"input id","parts":[{"text":"exact original span","kind":"current_outcome|historical_outcome|plan|availability|other","outcome":"performed|not_performed|unknown|none","extent":"operation|whole_run|none"}]}]}.
current_outcome means the text ASSERTS a trading or memory-writing result in this run, including categorical non-execution. For it, outcome must be performed or not_performed, and extent must distinguish a specific operation from an assertion covering the whole run. A specific rejected attempt is not an assertion that no actions occurred anywhere in the run.
availability is a statement about supplied records or an unconfirmed outcome, not a categorical assertion of execution/non-execution. Other kinds use outcome=none and extent=none. Include every input paragraph exactly once. Never use evidence or your knowledge to repair the meaning of the author's words."""


def validate_body_meaning(value, paragraphs):
    """Exact text reconstruction checks coverage, not the truth of prose.

    Semantic categories come from the body-only local model. No phrase list,
    keyword detector or sentence regex assigns these categories in the runtime.
    """
    if not isinstance(value, dict) or set(value) != {"paragraphs"}:
        raise ValueError("invalid body meaning response")
    rows = value["paragraphs"]
    if not isinstance(rows, list) or len(rows) != len(paragraphs):
        raise ValueError("body paragraph coverage differs")
    result = {}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"id", "parts"}
                or not isinstance(row["id"], str) or row["id"] not in paragraphs or row["id"] in result
                or not isinstance(row["parts"], list) or not 1 <= len(row["parts"]) <= 100):
            raise ValueError("invalid body partition")
        for part in row["parts"]:
            if (not isinstance(part, dict) or set(part) != {"text", "kind", "outcome", "extent"}
                    or any(not isinstance(part[key], str) for key in part) or not part["text"]
                    or part["kind"] not in {"current_outcome", "historical_outcome", "plan", "availability", "other"}
                    or part["outcome"] not in {"performed", "not_performed", "unknown", "none"}
                    or part["extent"] not in {"operation", "whole_run", "none"}):
                raise ValueError("invalid body part")
            if part["kind"] == "current_outcome":
                if part["outcome"] not in {"performed", "not_performed"} or part["extent"] not in {"operation", "whole_run"}:
                    raise ValueError("invalid current outcome meaning")
        if "".join(part["text"] for part in row["parts"]) != paragraphs[row["id"]]:
            raise ValueError("body partition omitted or rewrote text")
        result[row["id"]] = row["parts"]
    return result
