"""Apply bounded semantic edits without rewriting neighboring reader content."""
from __future__ import annotations

from trade_compass_agent.runtime.report_contract import (
    Gap, Paragraph, ReportDocument, paragraph_segments, paragraph_text,
)
from trade_compass_agent.runtime.report_transport import parse_report_items


def _parts_between(segments, start, stop):
    """Slice literal prose, retaining complete runtime-owned value bindings."""
    parts, cursor = [], 0
    for segment in segments:
        end = cursor + len(segment['text'])
        left, right = max(start, cursor), min(stop, end)
        if left < right:
            if segment['origin'] == 'evidence':
                if left != cursor or right != end:
                    raise ValueError('semantic edit splits an atomic value binding')
                parts.append({'type': 'value', 'ref': segment['ref'], 'field': segment['field']})
            else:
                parts.append({'type': 'text', 'text': segment['text'][left-cursor:right-cursor]})
        cursor = end
    if not 0 <= start < stop <= cursor:
        raise ValueError('invalid semantic edit extent')
    return parts


def assertion_edit_plan(document, catalog, statements, owners, failed, *, optional_unchanged=None):
    """Address failed assertions only when the original bindings remain atomic.

    Categories and verdicts come from completed upstream review. Text addressing
    here establishes neither a claim's meaning nor its truth.
    """
    by_id = {row['id']: row for row in statements}
    if not any(failed.intersection(ids) and set(ids) - failed for ids in owners.values()):
        return None  # A wholly failed paragraph still uses the existing full revision.
    edits, paragraphs = [], []
    try:
        for item in document.items:
            ids = owners.get(item.id, [])
            if not failed.intersection(ids):
                continue
            if not isinstance(item, Paragraph):
                return None
            text = paragraph_text(item, document, catalog)
            if ''.join(by_id[key]['text'] for key in ids) != text:
                return None  # Execution projections or omitted framing need the existing full revision.
            segments = paragraph_segments(item, document, catalog)
            spans, cursor = [], 0
            for key in ids:
                original = by_id[key]['text']
                stop = cursor + len(original)
                _parts_between(segments, cursor, stop)
                span = {'id': key, 'start': cursor, 'stop': stop, 'text': original}
                spans.append(span)
                if key in failed:
                    edits.append({'id': key, 'paragraph_id': item.id, 'text': original,
                                  'change_required': key not in (optional_unchanged or set())})
                cursor = stop
            paragraphs.append({'id': item.id, 'spans': spans})
    except (ValueError, KeyError):
        return None
    count = len(document.items) + sum(len(row['spans']) - 1 for row in paragraphs)
    if not edits or count > 100:
        return None
    return {'edits': edits, 'paragraphs': paragraphs}


def assertion_edit_schema(plan, parts_schema):
    return {'type': 'object', 'additionalProperties': False,
        'required': ['schema_version', 'edits', 'gaps'], 'properties': {
            'schema_version': {'const': 1},
            'edits': {'type': 'array', 'minItems': len(plan['edits']), 'maxItems': len(plan['edits']),
                'items': {'type': 'object', 'additionalProperties': False,
                    'required': ['id', 'parts', 'refs'], 'properties': {
                        'id': {'enum': [row['id'] for row in plan['edits']]},
                        'parts': {'type': 'array', 'maxItems': 80, 'items': parts_schema},
                        'refs': {'type': 'array', 'maxItems': 30, 'uniqueItems': True,
                                 'items': {'type': 'string'}}}}},
            'gaps': {'type': 'array', 'maxItems': 20, 'items': {
                'type': 'object', 'additionalProperties': False,
                'required': ['id', 'type', 'section', 'text'], 'properties': {
                    'id': {'type': 'string', 'minLength': 1, 'maxLength': 80},
                    'type': {'const': 'gap'}, 'section': {'const': 'gaps'},
                    'text': {'type': 'string', 'minLength': 1, 'maxLength': 1000}}}},
        }}


def apply_assertion_edits(value, revision, transport, catalog):
    """Reassemble original runs and changed spans, then let the normal review run.

    Split affected paragraphs at their already interpreted assertion boundaries.
    A failed replacement can then withhold its own paragraph without hiding the
    unchanged adjacent observations. Every resulting paragraph is reviewed anew.
    """
    plan = revision['assertion_edits']
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'edits', 'gaps'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or not isinstance(value['edits'], list) or not isinstance(value['gaps'], list)
            or len(value['gaps']) > 20):
        raise ValueError('invalid assertion edits')
    expected = {row['id'] for row in plan['edits']}
    supplied = {}
    for row in value['edits']:
        if (not isinstance(row, dict) or set(row) != {'id', 'parts', 'refs'}
                or not isinstance(row['id'], str) or row['id'] not in expected
                or row['id'] in supplied or not isinstance(row['parts'], list)):
            raise ValueError('invalid assertion edit identity')
        supplied[row['id']] = row
    if set(supplied) != expected:
        raise ValueError('assertion edit coverage differs')
    previous = ReportDocument.model_validate(transport.decode_document(revision['previous_document']))
    plans = {row['id']: row['spans'] for row in plan['paragraphs']}
    reserved = {item.id for item in previous.items} | set(catalog.facts) | set(transport.references)
    serial, items, changed = 0, [], False
    required = {row['id'] for row in plan['edits'] if row.get('change_required', True)}
    for item in previous.items:
        if item.id not in plans:
            items.append(item.model_dump())
            continue
        original = paragraph_text(item, previous, catalog)
        segments = paragraph_segments(item, previous, catalog)
        for span in plans[item.id]:
            if original[span['start']:span['stop']] != span['text']:
                raise ValueError('semantic edit no longer addresses original text')
            parts = _parts_between(segments, span['start'], span['stop'])
            refs = item.refs
            if span['id'] in supplied:
                patch = supplied[span['id']]
                # Empty replacements remove an unsupported assertion, not its neighbors.
                if not patch['parts']:
                    if patch['refs'] != []:
                        raise ValueError('empty semantic edit has references')
                    changed = True
                    continue
                candidate = {'schema_version': 1, 'items': [{
                    'id': 'replacement', 'type': 'paragraph', 'section': item.section,
                    'parts': patch['parts'], 'refs': patch['refs']}]}
                decoded, _, errors = parse_report_items(transport.decode_document(candidate))
                if errors or len(decoded.items) != 1 or not isinstance(decoded.items[0], Paragraph):
                    raise ValueError('invalid semantic edit parts')
                replacement = decoded.items[0]
                if paragraph_text(replacement, previous, catalog) == span['text']:
                    if span['id'] in required:
                        raise ValueError('semantic edit did not change its failed assertion')
                    # Retaining an optional opinion is not a new binding or a
                    # repaired claim. Preserve the original runs and provenance;
                    # genuine neighboring corrections change its reader context.
                else:
                    changed = True
                    parts = [part.model_dump() for part in replacement.parts]
                    refs = list(dict.fromkeys([*refs, *replacement.refs]))
            while True:
                identifier = f'revision-span-{serial}'
                serial += 1
                if identifier not in reserved:
                    reserved.add(identifier)
                    break
            items.append({'id': identifier, 'type': 'paragraph', 'section': item.section,
                          'parts': parts, 'refs': refs})
    if not changed:
        raise ValueError('semantic edits did not change the reader body')
    for row in value['gaps']:
        gap = Gap.model_validate(row)
        if gap.section != 'gaps' or gap.id in reserved:
            raise ValueError('new gap reuses an existing report identity')
        reserved.add(gap.id)
        items.append(gap.model_dump())
    # Structural and binding checks on this complete candidate still belong to
    # the existing publisher; no patch response itself constitutes approval.
    return {'schema_version': 1, 'items': items}
