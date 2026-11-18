"""Necessary source admissibility, never a semantic approval or prose classifier."""
from __future__ import annotations

import json

from trade_compass_agent.runtime.report_contract import PORTFOLIO_OPERATIONS


def distribution_requirements(statement):
    return [claim for claim in statement.get('classifications', [])
            if claim['property'] == 'distribution_basis']


def source_requirements(statement, assessment=None):
    claims = [c for c in statement.get('classifications', [])
            if c['property'] in {'distribution_basis', 'participant_activity'}]
    commitments = (assessment or {}).get('commitments', {})
    if (statement.get('assertion_kind') == 'actor_activity'
            or isinstance(commitments, dict) and commitments.get('participant_activity') is True):
        if not any(c['property'] == 'participant_activity' for c in claims):
            claims.append({'property': 'participant_activity', 'value': 'observed_actor'})
    return claims


def check_source_properties(statement, context, catalog, source_review, assessment=None):
    """Keep blind reader requirements when a later reviewer drops a modifier.

    Canonical method records are derived from owned observations. Original
    document text can also carry a method, but the semantic reviewer must name
    that quotation, not a price, a document title or a first-party scan receipt.
    This checks only source family and ownership. Relevance, dates, price levels
    and the conclusion itself retain their independent semantic checks.
    """
    requirements = source_requirements(statement, assessment)
    result = {'status': 'not_applicable', 'reason': 'no_source_property_requirement', 'claims': []}
    if not requirements:
        return result
    bound = [catalog.resolve(ref) for ref in statement['bound_fact_refs']]
    entities = {fact.entity for fact in bound if fact.entity}
    if not entities:
        entities = {catalog.resolve(ref).entity for ref in statement.get('paragraph_bound_refs', [])
                    if catalog.resolve(ref).entity}
    if not entities:
        entities = {fact['entity'] for fact in context['evidence'] if fact.get('entity')}
    refs = source_review.get('evidence_refs', []) if source_review else []
    invalid = [ref for ref in refs if ref not in catalog.facts]
    quotations = [catalog.resolve(ref) for ref in refs if ref in catalog.facts
        and catalog.resolve(ref).kind == 'quotation'
        and catalog.resolve(ref).metric in {'content', 'text', 'snippet', 'excerpt'}]
    methods = catalog.source_semantics()
    for requirement in requirements:
        check = {'claim': requirement, 'status': 'unknown', 'reason': 'distribution_source_missing', 'evidence': []}
        result['claims'].append(check)
        if invalid:
            check.update(status='unavailable', reason='source_property_reference_unavailable')
            continue
        if requirement['property'] == 'participant_activity':
            # Anonymous activity/price observations are not group observations.
            # Admissibility is necessary only; the source reviewer still checks
            # the exact group, gross/net measure, date and asserted scope.
            witnesses = []
            for ref in refs:
                if ref not in catalog.facts:
                    continue
                fact = catalog.resolve(ref)
                scope = json.loads(fact.scope) if fact.scope else {}
                if (fact in quotations
                        or requirement['value'] == 'observed_actor' and fact.source == 'get_events'
                        and fact.kind == 'quotation' and fact.metric == 'title'
                        or requirement['value'] != 'excluded' and fact.kind == 'flow'
                        and scope.get('category') not in {'行业', '概念'}
                        or requirement['value'] == 'observed_actor'
                        and (fact.source in PORTFOLIO_OPERATIONS | {'analyze_portfolio'}
                             or scope.get('trade_id'))):
                    witnesses.append(fact)
            bound_entities = {fact.entity for fact in bound if fact.entity}
            witnesses = [fact for fact in witnesses if not bound_entities or fact.entity in bound_entities
                or not fact.entity and len(bound_entities) == 1]
            check.update(reason='participant_source_missing')
            if witnesses and all(any(f.entity == e or not f.entity for f in witnesses) for e in bound_entities):
                check.update(status='entailed', reason='source_family_admissible_not_semantic_approval',
                             evidence=[fact.id for fact in witnesses])
            continue
        if not bound and len(entities) != 1:
            # Independent reviewers may identify an anaphoric source in a
            # mixed context. Agreement nominates ownership, not truth.
            agreed = set(refs) & set((assessment or {}).get('time_evidence', []))
            identified = {catalog.resolve(ref).entity for ref in agreed if ref in catalog.facts
                          and catalog.resolve(ref).entity}
            if identified:
                entities = identified
        # Multiple bound instruments are not silently replaced with whichever
        # one has a convenient method. Each owned instrument needs a witness.
        if not entities or not bound and len(entities) != 1:
            check['reason'] = 'distribution_source_scope_ambiguous'
            continue
        witnessed = {}
        for entity in entities:
            owned = [ref for method in methods if method['entity'] == entity
                and method['source_method'] == 'chip_cost_distribution'
                and requirement['value'] != 'measured' for ref in method['fact_refs']]
            quoted = [fact.id for fact in quotations if fact.entity == entity
                or not fact.entity and len(entities) == 1]
            if owned or quoted:
                witnessed[entity] = list(dict.fromkeys([*owned, *quoted]))
        if set(witnessed) == entities:
            check.update(status='entailed', reason='source_family_admissible_not_semantic_approval',
                         evidence=list(dict.fromkeys(ref for ids in witnessed.values() for ref in ids)))
    statuses = {claim['status'] for claim in result['claims']}
    result.update(status='unavailable' if 'unavailable' in statuses else
                  'unknown' if 'unknown' in statuses else 'entailed', reason='source_properties_checked')
    return result
