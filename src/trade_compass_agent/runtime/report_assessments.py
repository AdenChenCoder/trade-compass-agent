"""Keep per-assertion protocol failure separate from semantic rejection."""
from __future__ import annotations

from copy import deepcopy


DIMENSIONS = ('identity_scope', 'time', 'quantity', 'support')
DIMENSION_RESULTS = {'entailed', 'contradicted', 'unknown', 'not_applicable'}
VERDICTS = {'entailed', 'contradicted', 'unknown'}
CORE_FIELDS = {'id', 'comparison', 'checks', 'time_evidence', 'verdict'}
OPTIONAL_FIELDS = ('support_basis', 'commitments')


def approved_source_review_ids(assessments, errors):
    """A first approval cannot grant its own exemption from independent review.

    Neither a direct-support label nor a mistaken non-empirical classification
    proves that the author's exact text contains no additional factual premise.
    The existing second role checks it independently; genuine plans need no
    invented empirical proof.
    """
    return {row['id'] for row in assessments if row['id'] not in errors
            and row['verdict'] == 'entailed'}


def assessments_with_availability(value, statement_ids):
    """An invalid envelope/identity cannot be attributed; row faults can.

    Unavailable rows receive no semantic approval or completed rejection.
    Preserve their exact model fields for diagnostics and retain independent
    valid siblings. The publication owner still validates support metadata,
    temporal ownership, calculations and all positive body commitments.
    """
    if not isinstance(value, dict) or set(value) != {'assessments'}:
        raise ValueError('invalid factual review envelope')
    rows = value['assessments']
    if (not isinstance(rows, list) or len(rows) != len(statement_ids)
            or any(not isinstance(row, dict) or not isinstance(row.get('id'), str) for row in rows)
            or len({row['id'] for row in rows}) != len(rows)
            or {row['id'] for row in rows} != set(statement_ids)):
        raise ValueError('invalid factual review identity coverage')
    result, errors = [], {}
    for original in rows:
        row = deepcopy(original)
        faults = []
        if not CORE_FIELDS <= row.keys() or row.keys() - (CORE_FIELDS | set(OPTIONAL_FIELDS)):
            faults.append('fields')
        if (not isinstance(row.get('comparison'), str) or not row['comparison'].strip()
                or len(row['comparison']) > 5000):
            faults.append('comparison')
        if not isinstance(row.get('verdict'), str) or row['verdict'] not in VERDICTS:
            faults.append('verdict')
        refs = row.get('time_evidence')
        valid_refs = (isinstance(refs, list) and len(refs) <= 100
                      and all(isinstance(ref, str) for ref in refs) and len(refs) == len(set(refs)))
        if not valid_refs:
            faults.append('time_evidence')
        checks = row.get('checks')
        if not isinstance(checks, dict) or set(checks) != set(DIMENSIONS):
            faults.append('checks')
        elif any(not isinstance(status, str) or status not in DIMENSION_RESULTS for status in checks.values()):
            faults.append('checks')
        if faults:
            errors[row['id']] = {'stage': 'factual_review', 'code': 'factual_assessment_metadata_invalid',
                                 'fields': faults}
            # Unknown denotes availability here, not a successful factual
            # rejection. No invalid enum is interpreted or repaired into truth.
            comparison = (row['comparison'] if 'comparison' not in faults
                          else '该项研判尚未完成核验。')
            row = {key: row[key] for key in OPTIONAL_FIELDS if key in row} | {
                'id': original['id'], 'comparison': comparison,
                'time_evidence': refs if valid_refs else [],
                'checks': dict.fromkeys(DIMENSIONS, 'unknown'), 'verdict': 'unknown',
                'reported_checks': deepcopy(original.get('checks')),
                'reported_verdict': original.get('verdict'),
                'raw_assessment': deepcopy(original), 'validation_status': 'unavailable'}
        result.append(row)
    return result, errors
