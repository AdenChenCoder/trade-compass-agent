"""Classify reader assertions without source evidence or prose truth heuristics."""
from __future__ import annotations


BODY_MEANING = """Read ONLY the supplied reader paragraphs. You have no execution evidence and must not decide truth or justify a claim.
Classify assertions about the execution of an actor's state-changing operations: submitting, filling, rejecting or cancelling orders, or saving/deleting memory records. The classification target is an operation performed by an actor, NOT financial performance or observations of market/account state. Affirmative and negative outcomes both count. Do not soften a categorical outcome into uncertainty, or turn missing receipts into a negative outcome. Proposals, conditions, historical events, and inability to confirm an outcome are different meanings.
Partition EACH input text into contiguous parts at assertion boundaries. Copy text exactly, including punctuation/spacing; concatenating parts must reproduce the input byte-for-byte. Do not rewrite, normalize, add qualifiers, or omit any text. Split independent assertions when possible; retain sufficient original context to classify their meaning.
Return JSON: {"quantity_issues":[{"id":"paragraph id","text":"exact problematic quantity assertion span","reason":"missing unit or ratio scale in the reader body"}],"paragraphs":[{"id":"input id","parts":[{"text":"exact original span","kind":"current_outcome|historical_outcome|plan|availability|other","outcome":"performed|not_performed|unknown|none","extent":"operation|whole_run|none"}]}]}.
current_outcome means the text ASSERTS that an actor's state-changing operation was performed, rejected, or not performed in this run, including categorical non-execution. Price movement, return, valuation, holdings and balance observations are other, even when they describe the current run or changes over time; they do not themselves assert that the actor submitted an order or wrote memory. historical_outcome concerns a past state-changing operation, not a past price observation. For it, outcome must be performed or not_performed, and extent must distinguish a specific operation from an assertion covering the whole run. A specific rejected attempt is not an assertion that no actions occurred anywhere in the run.
Resolve the proposition the author actually commits to, preserving negation, conditions and embedding. A decision about representing an operation in a report is not a decision about performing that operation. Mentioning an operation as the object of a reporting rule does not assert its occurrence or non-occurrence. Conversely, affirming a business outcome still commits to that outcome even when introduced as a confirmation or explanation. Classify the asserted business state, not the occurrence of an operation word inside another proposition.
availability is a statement about supplied records or an unconfirmed outcome, not a categorical assertion of execution/non-execution. Other kinds use outcome=none and extent=none. Include every input paragraph exactly once. Never use evidence or your knowledge to repair the meaning of the author's words.
Independently inspect whether the reader body itself establishes the meaning of each stated quantity: its dimensional unit and, for ratios/rates, its numerical scale. Do not calculate an implied intended number from other values, use outside conventions to supply a missing percentage sign, or repair what the author probably intended. A dimensional quantity needs a unit established in its assertion or an explicit shared convention in these paragraphs. A bare ratio/rate numeral without an explicit fraction, ratio scale or percent convention is ambiguous. Intrinsically dimensionless multiples such as valuation ratios do not require a currency or percent unit. Securities identifiers, dates, counts and qualitative directions are not missing-unit quantities.
quantity_issues lists ONLY quantity meaning not established by the body. Copy an exact existing text span with its paragraph id for each issue. Use [] when all quantities are clear. This is NOT evidence review: an unambiguously stated factual error must not be marked as a quantity clarity issue. All paragraphs form one reader context; an explicitly shared unit/scale remains valid across clauses and paragraphs. Do not require repeated units or a fixed phrase. Do not infer a missing convention from evidence, calculated answers, or numerical plausibility."""


def validate_body_meaning(value, paragraphs):
    """Exact text reconstruction checks coverage, not the truth of prose.

    Semantic categories come from the body-only local model. No phrase list,
    keyword detector or sentence regex assigns these categories in the runtime.
    """
    if not isinstance(value, dict) or set(value) != {"paragraphs", "quantity_issues"}:
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
    issues = value["quantity_issues"]
    if not isinstance(issues, list) or len(issues) > 100:
        raise ValueError("invalid quantity meaning issues")
    by_id = {}
    for issue in issues:
        if (not isinstance(issue, dict) or set(issue) != {"id", "text", "reason"}
                or any(not isinstance(issue[key], str) or not issue[key].strip() for key in issue)
                or issue["id"] not in paragraphs or issue["text"] not in paragraphs[issue["id"]]
                or len(issue["reason"]) > 500):
            raise ValueError("invalid quantity meaning issue")
        by_id.setdefault(issue["id"], []).append(issue)
    return result, by_id


def runtime_bound_quantity_issue(issue, segments):
    """Locate a quoted span in owned output; never classify author words.

    A model's missing-unit allegation cannot override an explicit value_with_unit
    binding. If the quote also occurs in author prose, its location is ambiguous
    and the allegation remains. This proves unit presence only, never sentence
    meaning, factual scope, or the correctness of an author's quantity.
    """
    text = "".join(segment["text"] for segment in segments)
    boundaries, offset = [], 0
    for segment in segments:
        end = offset + len(segment["text"])
        if (segment["origin"] == "evidence" and segment.get("field") == "value_with_unit"
                and segment.get("quantity_unit") and segment["quantity_unit"] != "单位未提供"):
            boundaries.append((offset, end))
        offset = end
    cursor, locations = 0, []
    while (start := text.find(issue["text"], cursor)) >= 0:
        locations.append((start, start + len(issue["text"])))
        cursor = start + 1
    return bool(locations) and all(any(a <= start and end <= b for a, b in boundaries) for start, end in locations)
