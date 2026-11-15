"""Classify reader assertions without source evidence or prose truth heuristics."""
from __future__ import annotations


BODY_MEANING = """Read ONLY the supplied reader paragraphs. No task instructions, execution evidence or source records are supplied. Interpret the author's exact commitments; do not decide truth or repair the meaning to fit your knowledge.
Partition EACH input text by selecting the supplied source_units, not by copying or rewriting text. Each part supplies through, the exact ID of its last unit. The first part starts at the FIRST unit, and every next part starts immediately after the previous through unit. End the final part at the LAST supplied unit. This covers every original character, including framing, punctuation and spacing; there is no way to skip a prefix or a gap. Do not return text, character offsets or new units. Source-unit boundaries are mechanical addressing aids, not truth or meaning labels. Combine adjacent units to keep negation, conditions and embedding attached; split independent assertions where a unit boundary permits.
Each part should contain an independently assessable predicate, including conjoined factual, causal, actor-behavior and qualitative assertions. Do not produce one part per source unit: join prefixes, connectives and argument fragments to the predicate they belong to. Keep negation, conditions and embedding attached to the proposition they qualify. Do not group a new conclusion with neighboring measurements merely because they share a sentence; the original paragraph remains available to resolve fragments.
Independently identify what EACH part commits to before any evidence is seen. claim_kind is observation (a stated observed fact or available-record claim), comparison (a relationship between observations), actor_activity (an actor's identity, action or absence of action), causal_relation (one thing causing or explaining another), judgment (a recommendation or interpretation without an additional empirical commitment), or discourse (framing or a convention without a factual claim). This is meaning classification, NOT a truth verdict. Split changed commitment types where a source-unit boundary permits. When a unit contains several predicates, preserve them all and classify its empirical commitment rather than hiding observations or actor activity inside a judgment. An actor's asserted behavior remains actor_activity even when presented as technical analysis, an explanation or an opinion. Preserve qualifications and hypothetical scope; do not invent an assertion of actual activity from a proposed action or an inability to identify actors.
Classify the proposition, not its grammatical tense. An analyst's assessment of whether a holding rationale remains persuasive is a judgment, not a report of the investor's private mental state or a completed strategy change. Any separately asserted market premises remain observations, comparisons or causal/actor claims. An attribution of what an investor actually believed, decided or executed is a different proposition and must retain that commitment.
For each part independently return claim_kind: observation, comparison, actor_activity, causal_relation, judgment or discourse. This category is separate from operation_code. Recommendations and bounded interpretations are judgment unless the same part additionally asserts an empirical property; preserve that empirical commitment.
as_of_task_now is a boolean. Use true ONLY for an empirical observation the author asserts as current at the task's present business date, including a current value inside a judgment. It does not mean that a date is mentioned or can be referenced. A calendar-dated sample without an additional present-time assertion uses false. Referring back to an established calendar date keeps that calendar anchor. The report's topic and sample date do not assert present currency. Preserve a separate current-time assertion even beside an explicit date. Pure proposals, future watchpoints, discourse and a present opinion without a current empirical premise use false. You have no evidence from which to infer an observation time.
operation_code is ONE integer selecting a complete operation meaning. It concerns an actor's state-changing order or memory operation, not price/return/valuation/holding/balance observations. Market movements are code 0 even when current. These labels classify meaning, not whether the claim is true:
0: No assertion about a business operation, and no proposal or unconfirmed operation.
1: A proposed action, recommendation, conditional watchpoint or authored choice/decision; it does not itself assert execution. This is usually a judgment; retain separately asserted empirical premises.
2: Availability of operation records or an explicitly unconfirmed operation; no categorical execution/non-execution assertion.
3: A specific operation is asserted performed in THIS run.
4: A specific operation is asserted not performed, rejected or unsuccessful in THIS run.
5: An affirmative operation outcome is asserted for the whole THIS run.
6: Categorical non-execution is asserted for the whole THIS run.
7: A specific HISTORICAL operation is asserted performed.
8: A specific HISTORICAL operation is asserted not performed, rejected or unsuccessful.
9: An affirmative operation outcome is asserted for a whole HISTORICAL run.
10: Categorical non-execution is asserted for a whole HISTORICAL run.
A decision to continue holding or refrain from buying is code 1, not confirmation that an order did or did not occur. Split an additional completed-operation assertion where a source-unit boundary permits. A rejected attempt does not mean no actions occurred anywhere in the run. Do not soften categorical outcomes into uncertainty or turn absent records into non-execution.
Resolve the proposition the author commits to, preserving negation, conditions and embedding. Mentioning an operation as the subject of a reporting rule does not assert its occurrence or non-occurrence. Reporting an outcome, proposing an action and performing it are different meanings. A past price observation is not a historical order. Never use a business-operation label as claim_kind; select one of its six categories independently.
Return only JSON: {"quantity_issues":[{"id":"paragraph id","text":"exact problematic quantity span","reason":"missing unit or numerical scale"}],"paragraphs":[{"id":"input paragraph id","parts":[{"through":"exact source unit id","operation_code":0,"claim_kind":"observation","as_of_task_now":false}]}]}.
Include every paragraph exactly once and every original source unit in contiguous order. operation_code must be an integer from 0 through 10, claim_kind one of its six categories, and as_of_task_now true or false. Do not supply invented labels, copied reader text, evidence hints or extra fields.
Independently inspect whether the reader body itself establishes the meaning of each stated quantity: its dimensional unit and, for ratios/rates, its numerical scale. Do not calculate an implied intended number from other values, use outside conventions to supply a missing percentage sign, or repair what the author probably intended. A dimensional quantity needs a unit established in its assertion or an explicit shared convention in these paragraphs. A bare ratio/rate numeral without an explicit fraction, ratio scale or percent convention is ambiguous. Intrinsically dimensionless multiples such as valuation ratios do not require a currency or percent unit. Securities identifiers, dates, counts and qualitative directions are not missing-unit quantities.
quantity_issues lists ONLY quantity meaning not established by the body. Copy an exact existing text span with its paragraph id for each issue. Use [] when all quantities are clear. This is NOT evidence review: an unambiguously stated factual error must not be marked as a quantity clarity issue. All paragraphs form one reader context; an explicitly shared unit/scale remains valid across clauses and paragraphs. Do not require repeated units or a fixed phrase. Do not infer a missing convention from evidence, calculated answers, or numerical plausibility."""


class BodyMeaningError(ValueError):
    """Fixed validation causes, safe to retain without reader payloads."""


def _canonical_part(part):
    """Decode one complete operation state; never classify reader wording."""
    fields = {'operation_code', 'claim_kind', 'as_of_task_now'}
    if not isinstance(part, dict) or not fields.intersection(part):
        return part  # Existing selector and exact-quote responses remain readable.
    if set(part) != {'through', *fields}:
        raise BodyMeaningError('invalid_body_wire_part')
    operations = (
        ('other', 'none', 'none'), ('plan', 'none', 'none'), ('availability', 'unknown', 'none'),
        ('current_outcome', 'performed', 'operation'), ('current_outcome', 'not_performed', 'operation'),
        ('current_outcome', 'performed', 'whole_run'), ('current_outcome', 'not_performed', 'whole_run'),
        ('historical_outcome', 'performed', 'operation'), ('historical_outcome', 'not_performed', 'operation'),
        ('historical_outcome', 'performed', 'whole_run'), ('historical_outcome', 'not_performed', 'whole_run'),
    )
    if (type(part['operation_code']) is not int or not 0 <= part['operation_code'] < len(operations)
            or type(part['as_of_task_now']) is not bool):
        raise BodyMeaningError('invalid_body_wire_part')
    kind, outcome, extent = operations[part['operation_code']]
    return {'through': part['through'], 'kind': kind, 'assertion_kind': part['claim_kind'],
            'time_scope': 'reference_date' if part['as_of_task_now'] else 'other',
            'outcome': outcome, 'extent': extent}


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
        parts = [_canonical_part(part) for part in parts]
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
