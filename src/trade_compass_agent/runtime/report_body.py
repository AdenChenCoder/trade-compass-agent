"""Classify reader assertions without source evidence or prose truth heuristics."""
from __future__ import annotations


BODY_MEANING = """Read ONLY the supplied reader paragraphs. You have no execution evidence and must not decide truth or justify a claim.
Classify assertions about the execution of an actor's state-changing operations: submitting, filling, rejecting or cancelling orders, or saving/deleting memory records. The classification target is an operation performed by an actor, NOT financial performance or observations of market/account state. Affirmative and negative outcomes both count. Do not soften a categorical outcome into uncertainty, or turn missing receipts into a negative outcome. Proposals, conditions, historical events, and inability to confirm an outcome are different meanings.
Partition EACH input text by selecting the supplied source_units, not by copying or rewriting text. Each part supplies through, the exact ID of its last unit. The first part starts at the FIRST unit, and every next part starts immediately after the previous through unit. End the final part at the LAST supplied unit. This covers every original character, including framing, punctuation and spacing; there is no way to skip a prefix or a gap. Do not return text, character offsets or new units. Source-unit boundaries are mechanical addressing aids, not truth or meaning labels. Combine adjacent units to keep negation, conditions and embedding attached; split independent assertions where a unit boundary permits.
Each part should contain an independently assessable predicate, including conjoined factual, causal, actor-behavior and qualitative assertions. Do not produce one part per source unit: join prefixes, connectives and argument fragments to the predicate they belong to. Keep negation, conditions and embedding attached to the proposition they qualify. Do not group a new conclusion with neighboring measurements merely because they share a sentence; the original paragraph remains available to resolve fragments.
Independently identify what EACH part commits to before any evidence is seen. assertion_kind is observation (a stated observed fact or available-record claim), comparison (a relationship between observations), actor_activity (an actor's identity, action or absence of action), causal_relation (one thing causing or explaining another), judgment (a recommendation or interpretation without an additional empirical commitment), or discourse (framing or a convention without a factual claim). This is meaning classification, NOT a truth verdict. Split changed commitment types where a source-unit boundary permits. When a unit contains several predicates, preserve them all and classify its empirical commitment rather than hiding observations or actor activity inside a judgment. An actor's asserted behavior remains actor_activity even when presented as technical analysis, an explanation or an opinion. Preserve qualifications and hypothetical scope; do not invent an assertion of actual activity from a proposed action or an inability to identify actors.
Classify the proposition, not its grammatical tense. An analyst's assessment of whether a holding rationale remains persuasive is a judgment, not a report of the investor's private mental state or a completed strategy change. Any separately asserted market premises remain observations, comparisons or causal/actor claims. An attribution of what an investor actually believed, decided or executed is a different proposition and must retain that commitment.
Classify observation time BEFORE seeing any evidence. time_scope is reference_date when the author asserts an observation as of this task's present business date, including a current value embedded in a judgment. This fixes the author's commitment; it cannot later mean merely the latest available source. Use other for explicitly dated historical observations, comparisons spanning different dates, hypothetical thresholds, plans and discourse. This is the time of the asserted observation, not when the report is written or an operation proposed. Do not infer a time from sources, since none are supplied.
An opinion held now is not itself a current market observation. Use other for a judgment or causal explanation without an additional asserted current observation; classify its empirical premises separately. A current numeric value remains an observation even beside an opinion.
Return JSON: {"quantity_issues":[{"id":"paragraph id","text":"exact problematic quantity assertion span","reason":"missing unit or ratio scale in the reader body"}],"paragraphs":[{"id":"input id","parts":[{"through":"exact source unit id","kind":"current_outcome|historical_outcome|plan|availability|other","assertion_kind":"observation|comparison|actor_activity|causal_relation|judgment|discourse","time_scope":"reference_date|other","outcome":"performed|not_performed|unknown|none","extent":"operation|whole_run|none"}]}]}.
current_outcome means the text ASSERTS that an actor's state-changing operation was performed, rejected, or not performed in this run, including categorical non-execution. Price movement, return, valuation, holdings and balance observations are other, even when they describe the current run or changes over time; they do not themselves assert that the actor submitted an order or wrote memory. historical_outcome concerns a past state-changing operation, not a past price observation. For it, outcome must be performed or not_performed, and extent must distinguish a specific operation from an assertion covering the whole run. A specific rejected attempt is not an assertion that no actions occurred anywhere in the run.
Resolve the proposition the author actually commits to, preserving negation, conditions and embedding. A decision about representing an operation in a report is not a decision about performing that operation. Mentioning an operation as the object of a reporting rule does not assert its occurrence or non-occurrence. Conversely, affirming a business outcome still commits to that outcome even when introduced as a confirmation or explanation. Classify the asserted business state, not the occurrence of an operation word inside another proposition.
An authored choice, recommendation or decision about what to do is plan, including a decision made now to refrain from buying or continue holding. It does not by itself assert that an order was submitted or that execution/non-execution has been established. Preserve the scope of that intention across its dependent clauses. If a sentence additionally asserts a completed operation or a categorical execution outcome, split and classify that assertion independently; a decision does not exempt an accompanying outcome claim.
availability is a statement about supplied records or an unconfirmed outcome, not a categorical assertion of execution/non-execution. Other kinds use outcome=none and extent=none. Include every input paragraph exactly once. Never use evidence or your knowledge to repair the meaning of the author's words.
Independently inspect whether the reader body itself establishes the meaning of each stated quantity: its dimensional unit and, for ratios/rates, its numerical scale. Do not calculate an implied intended number from other values, use outside conventions to supply a missing percentage sign, or repair what the author probably intended. A dimensional quantity needs a unit established in its assertion or an explicit shared convention in these paragraphs. A bare ratio/rate numeral without an explicit fraction, ratio scale or percent convention is ambiguous. Intrinsically dimensionless multiples such as valuation ratios do not require a currency or percent unit. Securities identifiers, dates, counts and qualitative directions are not missing-unit quantities.
quantity_issues lists ONLY quantity meaning not established by the body. Copy an exact existing text span with its paragraph id for each issue. Use [] when all quantities are clear. This is NOT evidence review: an unambiguously stated factual error must not be marked as a quantity clarity issue. All paragraphs form one reader context; an explicitly shared unit/scale remains valid across clauses and paragraphs. Do not require repeated units or a fixed phrase. Do not infer a missing convention from evidence, calculated answers, or numerical plausibility."""


class BodyMeaningError(ValueError):
    """Fixed validation causes, safe to retain without reader payloads."""


def reader_source_units(text):
    """Address original text without deciding a sentence's meaning or truth."""
    pieces, start = [], 0
    for offset, char in enumerate(text):
        # Preserve separators and whitespace. A decimal point, URL or security
        # suffix is not an addressing break; ranges may combine any units.
        boundary = char in '，。！？；,;!?\n' or (
            char == '.' and (offset + 1 == len(text) or text[offset + 1].isspace()))
        if boundary:
            pieces.append(text[start:offset + 1])
            start = offset + 1
    if start < len(text):
        pieces.append(text[start:])
    return [{'id': f'u{index}', 'text': part} for index, part in enumerate(pieces)]


def validate_body_meaning(value, paragraphs):
    """Exact text reconstruction checks coverage, not the truth of prose.

    Semantic categories come from the body-only local model. No phrase list,
    keyword detector or sentence regex assigns these categories in the runtime.
    """
    if not isinstance(value, dict) or set(value) != {"paragraphs", "quantity_issues"}:
        raise BodyMeaningError("invalid body meaning response")
    rows = value["paragraphs"]
    if not isinstance(rows, list) or len(rows) != len(paragraphs):
        raise BodyMeaningError("body paragraph coverage differs")
    result = {}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"id", "parts"}
                or not isinstance(row["id"], str) or row["id"] not in paragraphs or row["id"] in result
                or not isinstance(row["parts"], list) or not 1 <= len(row["parts"]) <= 100):
            raise BodyMeaningError("invalid body partition")
        # Old responses may contain redundant copied referent hints. Never use
        # them to interpret or prove the reader's claim: all original reader
        # text already survives in the ordered source selections.
        parts = [{key: value for key, value in part.items() if key != 'referents'}
                 if isinstance(part, dict) else part for part in row['parts']]
        if any(isinstance(part, dict) and 'through' in part for part in parts):
            units = reader_source_units(paragraphs[row['id']])
            positions = {unit['id']: index for index, unit in enumerate(units)}
            selected, cursor = [], 0
            for part in parts:
                if (not isinstance(part, dict) or set(part) != {
                        'through', 'kind', 'assertion_kind', 'time_scope', 'outcome', 'extent'}
                        or not isinstance(part['through'], str) or part['through'] not in positions
                        or positions[part['through']] < cursor):
                    raise BodyMeaningError('invalid_source_unit_selection')
                stop = positions[part['through']] + 1
                selected.append({**{key: value for key, value in part.items() if key != 'through'},
                                 'text': ''.join(unit['text'] for unit in units[cursor:stop])})
                cursor = stop
            if cursor != len(units):
                raise BodyMeaningError('incomplete_source_unit_coverage')
            parts = selected
        # Exact quoted responses remain readable for compatibility, with the
        # original strict reconstruction check. New inference uses selectors.
        for part in parts:
            if (not isinstance(part, dict) or set(part) != {"text", "kind", "assertion_kind", "time_scope", "outcome", "extent"}
                    or any(not isinstance(part[key], str) for key in part) or not part["text"]
                    or part["kind"] not in {"current_outcome", "historical_outcome", "plan", "availability", "other"}
                    or part["assertion_kind"] not in {"observation", "comparison", "actor_activity", "causal_relation", "judgment", "discourse"}
                    or part['time_scope'] not in {'reference_date', 'other'}
                    or part["outcome"] not in {"performed", "not_performed", "unknown", "none"}
                    or part["extent"] not in {"operation", "whole_run", "none"}):
                raise BodyMeaningError("invalid body part")
            if part["kind"] == "current_outcome":
                if part["outcome"] not in {"performed", "not_performed"} or part["extent"] not in {"operation", "whole_run"}:
                    raise BodyMeaningError("invalid current outcome meaning")
        if "".join(part["text"] for part in parts) != paragraphs[row["id"]]:
            raise BodyMeaningError("body partition omitted or rewrote text")
        result[row["id"]] = parts
    issues = value["quantity_issues"]
    if not isinstance(issues, list) or len(issues) > 100:
        raise BodyMeaningError("invalid quantity meaning issues")
    by_id = {}
    for issue in issues:
        if (not isinstance(issue, dict) or set(issue) != {"id", "text", "reason"}
                or any(not isinstance(issue[key], str) or not issue[key].strip() for key in issue)
                or issue["id"] not in paragraphs or issue["text"] not in paragraphs[issue["id"]]
                or len(issue["reason"]) > 500):
            raise BodyMeaningError("invalid quantity meaning issue")
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


def factual_assertions(inferences, body_meaning):
    """Reuse the validated body partition; no new wording or truth heuristic.

    Evidence and paragraph context are stored once. Opaque runtime IDs separate
    assertion coverage from author-controlled paragraph and fact IDs.
    """
    contexts, statements, owners = {}, [], {}
    for row in inferences:
        identifier = row['id']
        context_id = f'p{len(contexts)}'
        # The candidate paragraph cannot masquerade as source evidence. Only
        # typed owned evidence lives here; reader meaning stays with its target.
        contexts[context_id] = {key: row[key] for key in ('evidence', 'calculations')}
        parts = (body_meaning or {}).get(identifier, [{'text': row['text'], 'kind': 'other', 'assertion_kind': 'observation', 'time_scope': 'other'}])
        kept = [part for part in parts if part['kind'] != 'current_outcome']
        if ''.join(part['text'] for part in kept) != row['text']:
            raise ValueError('factual assertion partition differs from reviewed body')
        owners[identifier] = []
        segments, cursor = [], 0
        for segment in row.get('owned_segments', row.get('segments', [])):
            end = cursor + len(segment['text'])
            segments.append((cursor, end, segment))
            cursor = end
        if any(s['origin'] == 'evidence' for _, _, s in segments) and ''.join(s['text'] for _, _, s in segments) != ''.join(p['text'] for p in parts):
            raise ValueError('owned value locations differ from interpreted body')
        offset = 0
        for part in parts:
            end = offset + len(part['text'])
            owned = [segment for start, stop, segment in segments if start < end and stop > offset
                     and segment['origin'] == 'evidence' and segment.get('field') == 'value_with_unit']
            offset = end
            if part['kind'] == 'current_outcome':
                continue
            claim_id = f'c{len(statements)}'
            statements.append({'id': claim_id, 'context_id': context_id, 'text': part['text'],
                               'assertion_kind': part['assertion_kind'], 'meaning_kind': part['kind'],
                               'time_scope': part['time_scope'],
                               'bound_fact_refs': list(dict.fromkeys(ref for s in owned for ref in s['fact_refs'])),
                               'time_fact_refs': list(dict.fromkeys(ref for s in owned for ref in s['date_refs']))})
            owners[identifier].append(claim_id)
    return contexts, statements, owners
