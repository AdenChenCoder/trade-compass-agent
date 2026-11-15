"""Check pre-evidence categorical commitments against owned indicator values."""
from __future__ import annotations

from decimal import Decimal
from trade_compass_agent.runtime.indicator_values import moving_average_alignment


def _observation_key(fact):
    return (fact.get('entity') or fact['subject'], *(fact.get(key, '') for key in
            ('account', 'source', 'timeframe', 'observed_at', 'scope')))


def check_classifications(statement, assessment, context):
    claims = statement.get('classifications', [])
    result = {'status': 'not_applicable', 'reason': 'no_categorical_commitment', 'claims': []}
    if not claims:
        return result
    facts = {fact['id']: fact for fact in context['evidence']}
    # Keep bindings authoritative. A reviewer cannot replace their security,
    # observation or timeframe with another convenient indicator receipt.
    bound = [facts[ref] for ref in statement['bound_fact_refs'] if ref in facts
             and facts[ref]['kind'] in {'moving_average', 'classification'}]
    witnesses = bound or [facts[ref] for ref in assessment['time_evidence'] if ref in facts
                          and facts[ref]['kind'] in {'moving_average', 'classification'}]
    candidates = witnesses or [f for f in facts.values() if f['source'] == 'compute_ma'
                              and f['kind'] in {'moving_average', 'classification'}]
    scopes = {_observation_key(f) for f in candidates}
    for claim in claims:
        check = {'claim': claim, 'status': 'unknown', 'reason': 'indicator_scope_ambiguous', 'evidence': []}
        result['claims'].append(check)
        if len(scopes) != 1:
            continue
        scope = next(iter(scopes))
        available = [f for f in facts.values() if _observation_key(f) == scope]
        selected = [f for f in candidates if f['kind'] == 'moving_average']
        if claim['scope'] == 'explicit_subset':
            # Periods are source-field identities, never numbers parsed out of prose.
            needed = {f'MA{period}' for period in claim['periods']}
            selected = [f for f in available if f['metric'] in needed
                        and f['kind'] == 'moving_average']
            if {f['metric'] for f in selected} != needed:
                check['reason'] = 'indicator_period_unavailable'
                continue
        elif len(selected) < 2:
            selected = [f for f in available if f['kind'] == 'moving_average']
        direct = [f for f in available if f['kind'] == 'classification' and f['metric'] == 'ma_alignment']
        if not selected and claim['scope'] == 'all_mentioned' and len({f['value'] for f in direct}) == 1:
            # A directly bound categorical result need not repeat every operand
            # in the reader paragraph. Its exact original receipt remains owned.
            selected, actual = direct, direct[0]['value']
        else:
            observations = {}
            for fact in selected:
                observations.setdefault(fact['metric'], set()).add(Decimal(fact['value']))
            if any(len(values) != 1 for values in observations.values()):
                check['reason'] = 'indicator_values_conflict'
                continue
            values = {metric: next(iter(numbers)) for metric, numbers in observations.items()}
            actual = moving_average_alignment(values)
        check.update(actual=actual, receipt_ids=sorted({f['receipt_id'] for f in selected}),
                     evidence=[f['id'] for f in selected])
        if actual == 'insufficient_data':
            check['reason'] = 'indicator_evidence_incomplete'
            continue
        matched = actual == claim['value']
        if claim.get('operator', 'eq') == 'ne':
            matched = not matched
        check.update(status='entailed' if matched else 'unknown',
                     reason='matched_indicator_classification' if matched else 'indicator_classification_conflict')
    result.update(status='entailed' if all(c['status'] == 'entailed' for c in result['claims']) else 'unknown',
                  reason='indicator_classifications_checked')
    return result
