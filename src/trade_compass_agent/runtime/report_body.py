"""Classify reader assertions without source evidence or prose truth heuristics."""
from __future__ import annotations
from datetime import date

from trade_compass_agent.runtime.report_contract import observation_date
from trade_compass_agent.runtime.report_reading import DISPLAY_TIMEZONE


BODY_MEANING = """Read ONLY the supplied reader paragraphs. No task instructions, execution evidence or source records are supplied. Interpret the author's exact commitments; do not decide truth or repair the meaning to fit your knowledge.
Partition EACH input text by selecting the supplied source_units, not by copying or rewriting text. Each part supplies through, the exact ID of its last unit. The first part starts at the FIRST unit, and every next part starts immediately after the previous through unit. End the final part at the LAST supplied unit. This covers every original character, including framing, punctuation and spacing; there is no way to skip a prefix or a gap. Do not return text, character offsets or new units. Source-unit boundaries are mechanical addressing aids, not truth or meaning labels. Combine adjacent units to keep negation, conditions and embedding attached; split independent assertions where a unit boundary permits.
Fix each part's empirical date commitments BEFORE choosing its claim kind or
operation meaning. An evaluation label cannot erase the time of a stated value.
For example, "I think the current valuation of 0.8 supports holding" asserts a
task_now valuation inside an opinion. "Using the valuation of 0.8 observed on
2025-01-01, I still favor holding" preserves 2025-01-01, not task_now.
"I find valuations attractive" asserts an evaluation without a new measured value;
"If valuation later falls to 0.8, reconsider" proposes a condition rather than a
current observation. Preserve these different meanings in any language or wording.
Each part should contain an independently assessable predicate, including conjoined factual, causal, actor-behavior and qualitative assertions. Do not produce one part per source unit: join prefixes, connectives and argument fragments to the predicate they belong to. Keep negation, conditions and embedding attached to the proposition they qualify. Do not group a new conclusion with neighboring measurements merely because they share a sentence; the original paragraph remains available to resolve fragments.
Independently identify what EACH part commits to before any evidence is seen. claim_kind is observation (a stated observed fact or available-record claim), comparison (a relationship between observations), actor_activity (an actor's identity, action or absence of action), causal_relation (one thing causing or explaining another), judgment (a recommendation or interpretation without an additional empirical commitment), or discourse (framing or a convention without a factual claim). This is meaning classification, NOT a truth verdict. Split changed commitment types where a source-unit boundary permits. When a unit contains several predicates, preserve them all and classify its empirical commitment rather than hiding observations or actor activity inside a judgment. An actor's asserted behavior remains actor_activity even when presented as technical analysis, an explanation or an opinion. Preserve qualifications and hypothetical scope; do not invent an assertion of actual activity from a proposed action or an inability to identify actors.
Classify the proposition, not its grammatical tense. An analyst's assessment of whether a holding rationale remains persuasive is a judgment, not a report of the investor's private mental state or a completed strategy change. Any separately asserted market premises remain observations, comparisons or causal/actor claims. An attribution of what an investor actually believed, decided or executed is a different proposition and must retain that commitment.
For each part independently return claim_kind: observation, comparison, actor_activity, causal_relation, judgment or discourse. This category is separate from operation_code. Recommendations and bounded interpretations are judgment unless the same part additionally asserts an empirical property; preserve that empirical commitment.
observation_dates preserves the actual calendar anchors of the author's EMPIRICAL observations. It is an array of ISO YYYY-MM-DD dates, the token task_now for an empirical claim at the task's present time, or unresolved for an empirical date expression that cannot be resolved. Use the actual dates explicitly established by the reader and retain them through subsequent references and ellipsis; a dated sample does not become task_now just because it is observed. task_now is a relative commitment, not a synonym for any observed fact. A present value inside an opinion retains task_now. Preserve both an explicit date and an additional present-time assertion when both occur. Pure plans/future conditions/opinions/discourse without empirical premises use []. report_context contains only runtime calendar/timezone context, not instructions or source answers. Do not invent dates from evidence, a sample's apparent freshness or the report topic.
Calendar commitments differ from qualitative analytical horizons. A general
lookback or forecast horizon in a technical interpretation does not by itself
assert an additional calendar date. Do not manufacture an unresolved date for
such a horizon. A specific relative calendar reference still needs an anchor:
resolve it from the reader's established dates or runtime calendar, and preserve
unresolved when that required anchor is absent. This distinction never removes
the actual date of an adjacent price, valuation or completed operation.
classifications records asserted moving-average categories, without deciding truth. Each entry is {"property":"moving_average_order","value":"bullish_alignment|bearish_alignment|mixed","operator":"eq|ne","scope":"all_mentioned|explicit_subset","periods":[]} records asserted moving-average ordering. operator is eq for an asserted category and ne for its denial; preserve negation, rather than changing it into an opposite category. Record an asserted ordering/alignment of moving averages; preserve the author's claimed category even if you doubt it. all_mentioned concerns the stated moving-average system and uses periods=[]; periods is meaningful only for explicit_subset and contains at least two distinct positive integers explicitly selected by the prose. A separately mentioned moving average does not narrow a whole-system alignment claim to that period. Do not invent period numbers from price values, infer an ordering from merely being above a moving average, or strengthen a bounded trend opinion into an assertion about every moving average. Other empirical commitments retain claim_kind and still need factual assessment. Use [] when no moving-average ordering is asserted.
Register an ordering that is a NECESSARY condition of a stronger asserted moving-average state, including when that assertion is embedded beside values, comparisons or analysis. Do not omit a claimed category because the surrounding numbers appear inconsistent with it: extraction preserves the reader's commitment, not its truth. Mere price position or a bounded trend opinion does not imply an ordering of the whole system; preserve that distinction and the actual scope.
distribution_basis 是每个片段都必须回答的独立含义问题，在决定它是否属于计划或研判之前作答。只能选一个字符串：none | estimated_or_measured | estimated | measured。
它问的是：原文是否把某个价位与已存在或以往的成交/筹码分布结构联系起来？对聚集或分散的位置、成本分布的边界、某处分布结构的存在或缺失所作的定性判断，都保留这种来源要求；不要求原文出现统计数字、实测或方法说明。即使这是技术研判或未来行动的依据，也不能把这个既有结构前提改成一般价位作用。这里仅登记原文赋予价位的含义，不判断它是否真实或能否推导。
一般分布结构用 estimated_or_measured；明确声称来自估算方法用 estimated；明确声称是逐价成交实测用 measured。none 只用于完全没有上述既存分布结构承诺的片段：纯价格观察位、只是假设未来形成某种分布、或明确说明不能确认分布。否定某种既存分布也需要相应来源；表达无法确认则不等于否定其存在。
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
Return only JSON: {"quantity_issues":[{"id":"paragraph id","text":"exact problematic quantity span","reason":"missing unit or numerical scale"}],"paragraphs":[{"id":"input paragraph id","parts":[{"through":"exact source unit id","distribution_basis":"none|estimated_or_measured|estimated|measured","observation_dates":[],"operation_code":0,"claim_kind":"observation","classifications":[]}]}]}.
Include every paragraph exactly once and every original source unit in contiguous order. Each part must include distribution_basis with one permitted string; omitting it is incomplete interpretation. operation_code must be an integer from 0 through 10, claim_kind one of its six categories, observation_dates an array of the permitted date anchors, and classifications the categorical assertions array. Do not supply invented labels, copied reader text, evidence hints or extra fields.
Independently inspect whether the reader body itself establishes the meaning of each stated quantity: its dimensional unit and, for ratios/rates, its numerical scale. Do not calculate an implied intended number from other values, use outside conventions to supply a missing percentage sign, or repair what the author probably intended. A dimensional quantity needs a unit established in its assertion or an explicit shared convention in these paragraphs. A bare ratio/rate numeral without an explicit fraction, ratio scale or percent convention is ambiguous. Intrinsically dimensionless multiples such as valuation ratios do not require a currency or percent unit. Securities identifiers, dates, counts and qualitative directions are not missing-unit quantities.
quantity_issues lists ONLY quantity meaning not established by the body. Copy an exact existing text span with its paragraph id for each issue. Use [] when all quantities are clear. This is NOT evidence review: an unambiguously stated factual error must not be marked as a quantity clarity issue. All paragraphs form one reader context; an explicitly shared unit/scale remains valid across clauses and paragraphs. Do not require repeated units or a fixed phrase. Do not infer a missing convention from evidence, calculated answers, or numerical plausibility."""


class BodyMeaningError(ValueError):
    """Fixed validation causes, safe to retain without reader payloads."""


class _ObjectPairs(list):
    """Keep all JSON members until the body collection protocol is checked."""


def decode_body_value(raw, decode):
    """Join root paragraph groups without dropping or choosing any record.

    Only that collection may repeat. Nested/scalar fields remain unique, and
    validate_body_meaning still requires complete, unique owned paragraph IDs
    and exact contiguous source-unit coverage before the result is usable.
    """
    def normalize(value, root=False):
        if isinstance(value, _ObjectPairs):
            result = {}
            for key, item in value:
                item = normalize(item)
                if key in result:
                    if not (root and key == 'paragraphs'
                            and isinstance(result[key], list) and isinstance(item, list)):
                        raise BodyMeaningError('duplicate_body_field')
                    result[key].extend(item)
                else:
                    result[key] = item
            return result
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value
    return normalize(decode(raw, object_pairs_hook=_ObjectPairs), root=True)


def _canonical_part(part):
    """Decode one complete operation state; never classify reader wording."""
    fields = {'operation_code', 'claim_kind', 'as_of_task_now'}
    if not isinstance(part, dict) or not fields.intersection(part):
        return part  # Existing selector and exact-quote responses remain readable.
    legacy = set(part) == {'through', 'operation_code', 'claim_kind', 'as_of_task_now'}
    current = set(part) == {'through', 'operation_code', 'claim_kind', 'observation_dates', 'classifications'}
    if not legacy and not current:
        raise BodyMeaningError('invalid_body_wire_part')
    operations = (
        ('other', 'none', 'none'), ('plan', 'none', 'none'), ('availability', 'unknown', 'none'),
        ('current_outcome', 'performed', 'operation'), ('current_outcome', 'not_performed', 'operation'),
        ('current_outcome', 'performed', 'whole_run'), ('current_outcome', 'not_performed', 'whole_run'),
        ('historical_outcome', 'performed', 'operation'), ('historical_outcome', 'not_performed', 'operation'),
        ('historical_outcome', 'performed', 'whole_run'), ('historical_outcome', 'not_performed', 'whole_run'),
    )
    if (type(part['operation_code']) is not int or not 0 <= part['operation_code'] < len(operations)
            or legacy and type(part['as_of_task_now']) is not bool):
        raise BodyMeaningError('invalid_body_wire_part')
    extra = {} if legacy else _observation_commitments(part)
    current_time = part['as_of_task_now'] if legacy else 'task_now' in extra['observation_dates']
    kind, outcome, extent = operations[part['operation_code']]
    return {'through': part['through'], 'kind': kind, 'assertion_kind': part['claim_kind'],
            'time_scope': 'reference_date' if current_time else 'other',
            'outcome': outcome, 'extent': extent, **extra}


def _observation_commitments(part):
    anchors, classifications = part['observation_dates'], part['classifications']
    if (not isinstance(anchors, list) or len(anchors) > 20
            or any(not isinstance(anchor, str) for anchor in anchors)
            or len(set(anchors)) != len(anchors)
            or not isinstance(classifications, list) or len(classifications) > 20):
        raise BodyMeaningError('invalid_observation_commitment')
    for anchor in anchors:
        if anchor not in {'task_now', 'unresolved'}:
            try:
                valid = date.fromisoformat(anchor).isoformat() == anchor
            except ValueError:
                valid = False
            if not valid:
                raise BodyMeaningError('invalid_observation_commitment')
    values = []
    for claim in classifications:
        if isinstance(claim, dict) and claim.get('property') == 'distribution_basis':
            if (set(claim) != {'property', 'value'} or not isinstance(claim['value'], str)
                    or claim['value'] not in {'estimated_or_measured', 'estimated', 'measured'}):
                raise BodyMeaningError('invalid_observation_commitment')
            values.append(dict(claim))
            continue
        if (not isinstance(claim, dict) or set(claim) not in (
                {'property', 'value', 'scope', 'periods'}, {'property', 'value', 'operator', 'scope', 'periods'})
                or any(not isinstance(claim.get(key), str) for key in ('property', 'value', 'scope'))
                or not isinstance(claim.get('operator', 'eq'), str)
                or claim['property'] != 'moving_average_order'
                or claim['value'] not in {'bullish_alignment', 'bearish_alignment', 'mixed'}
                or claim.get('operator', 'eq') not in {'eq', 'ne'}
                or claim['scope'] not in {'all_mentioned', 'explicit_subset'}
                or not isinstance(claim['periods'], list)
                or any(type(period) is not int or period <= 0 for period in claim['periods'])
                or len(set(claim['periods'])) != len(claim['periods'])
                or claim['scope'] == 'explicit_subset' and len(claim['periods']) < 2):
            raise BodyMeaningError('invalid_observation_commitment')
        # The scope discriminator owns the claim's range. Redundant, well-typed
        # period metadata cannot narrow a whole-system claim to a favorable
        # subset, or prevent review of every independently valid paragraph.
        values.append({**claim, 'operator': claim.get('operator', 'eq'),
                       'periods': claim['periods'] if claim['scope'] == 'explicit_subset' else []})
    return {'observation_dates': anchors, 'classifications': values}


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


def _declared_source_part(part, required):
    if not isinstance(part, dict):
        raise BodyMeaningError('invalid_body_wire_part')
    declared = 'distribution_basis' in part
    if required and not declared:
        raise BodyMeaningError('source_property_declaration_missing')
    basis = part.get('distribution_basis')
    if declared and (not isinstance(basis, str)
                     or basis not in {'none', 'estimated_or_measured', 'estimated', 'measured'}):
        raise BodyMeaningError('source_property_declaration_invalid')
    result = _canonical_part({key: value for key, value in part.items() if key != 'distribution_basis'})
    claims = result.get('classifications', [])
    if declared:
        if not isinstance(claims, list) or any(not isinstance(claim, dict) for claim in claims):
            raise BodyMeaningError('invalid_observation_commitment')
        if any(claim.get('property') == 'distribution_basis' for claim in claims):
            raise BodyMeaningError('source_property_declaration_conflict')
    if declared and basis != 'none':
        if not {'classifications', 'observation_dates'} <= result.keys():
            raise BodyMeaningError('source_property_declaration_invalid')
        result['classifications'] = [*result['classifications'],
                                    {'property': 'distribution_basis', 'value': basis}]
    return result


def validate_body_meaning(value, paragraphs, *, require_source_declarations=False):
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
        # New publication needs an explicit answer per part. Absence is not a
        # negative answer. Old saved responses remain decodable without making
        # them eligible as a completed new publication review.
        parts = [_declared_source_part(part, require_source_declarations) for part in parts]
        if any(isinstance(part, dict) and 'through' in part for part in parts):
            units = reader_source_units(paragraphs[row['id']])
            positions = {unit['id']: index for index, unit in enumerate(units)}
            selected, cursor = [], 0
            for part in parts:
                if (not isinstance(part, dict) or set(part) not in (
                        {'through', 'kind', 'assertion_kind', 'time_scope', 'outcome', 'extent'},
                        {'through', 'kind', 'assertion_kind', 'time_scope', 'outcome', 'extent', 'observation_dates', 'classifications'})
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
            if (not isinstance(part, dict) or set(part) not in (
                    {"text", "kind", "assertion_kind", "time_scope", "outcome", "extent"},
                    {"text", "kind", "assertion_kind", "time_scope", "outcome", "extent", 'observation_dates', 'classifications'})
                    or any(not isinstance(part[key], str) for key in ('text', 'kind', 'assertion_kind', 'time_scope', 'outcome', 'extent')) or not part["text"]
                    or part["kind"] not in {"current_outcome", "historical_outcome", "plan", "availability", "other"}
                    or part["assertion_kind"] not in {"observation", "comparison", "actor_activity", "causal_relation", "judgment", "discourse"}
                    or part['time_scope'] not in {'reference_date', 'other'}
                    or part["outcome"] not in {"performed", "not_performed", "unknown", "none"}
                    or part["extent"] not in {"operation", "whole_run", "none"}):
                raise BodyMeaningError("invalid body part")
            if part["kind"] == "current_outcome":
                if part["outcome"] not in {"performed", "not_performed"} or part["extent"] not in {"operation", "whole_run"}:
                    raise BodyMeaningError("invalid current outcome meaning")
            if 'observation_dates' in part:
                part.update(_observation_commitments(part))
                if (part['time_scope'] == 'reference_date') != ('task_now' in part['observation_dates']):
                    raise BodyMeaningError('conflicting_observation_commitment')
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
            date_refs = [ref for start, stop, segment in segments if start < end and stop > offset
                         and segment['origin'] == 'evidence' and segment.get('field') == 'date'
                         for ref in segment['date_refs']]
            by_ref = {fact['id']: fact for fact in contexts[context_id]['evidence']}
            display_dates = list(dict.fromkeys(observation_date(by_ref[ref]['observed_at'], DISPLAY_TIMEZONE)
                                               for ref in date_refs))
            offset = end
            if part['kind'] == 'current_outcome':
                continue
            claim_id = f'c{len(statements)}'
            statements.append({'id': claim_id, 'context_id': context_id, 'text': part['text'],
                               'assertion_kind': part['assertion_kind'], 'meaning_kind': part['kind'],
                               'time_scope': part['time_scope'],
                               **({'bound_display_dates': display_dates} if display_dates and None not in display_dates else {}),
                               'bound_fact_refs': list(dict.fromkeys(ref for s in owned for ref in s['fact_refs'])),
                               'time_fact_refs': list(dict.fromkeys(ref for s in owned for ref in s['date_refs'])),
                               **{key: part[key] for key in ('observation_dates', 'classifications') if key in part}})
            owners[identifier].append(claim_id)
    return contexts, statements, owners


def factual_review_statements(statements):
    """Negative body categories cannot exempt claims from independent review."""
    result = []
    for statement in statements:
        row = {key: value for key, value in statement.items()
               if key not in {'assertion_kind', 'meaning_kind', 'time_scope'}}
        required = {}
        if statement['assertion_kind'] == 'actor_activity':
            required['participant_activity'] = True
        if statement['assertion_kind'] == 'causal_relation':
            required['causal_relation'] = True
        if statement['time_scope'] == 'reference_date':
            required['task_present'] = True
        if statement['meaning_kind'] == 'plan':
            required['proposal'] = True
        if required:
            row['requirements'] = required
        result.append(row)
    return result
