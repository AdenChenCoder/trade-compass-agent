"""Check typed evidence use without interpreting or matching reader prose."""
from __future__ import annotations


SUPPORT_BASES = {"direct", "calculated", "reasoned", "unknown", "not_applicable"}


def check_support_basis(statement, assessment):
    """A basis is an assessor claim, never independent proof of sentence truth.

    Preserve the body-only empirical commitment. A calculation or analytical
    interpretation cannot be promoted into an observed actor or causal event.
    Missing metadata is unavailable review, not a completed factual rejection.
    """
    basis = assessment.get("support_basis")
    if not isinstance(basis, str) or basis not in SUPPORT_BASES:
        return {"status": "unavailable", "basis": None,
                "reason": "support_basis_missing" if basis is None else "support_basis_invalid"}
    kind = statement["assertion_kind"]
    commitments = assessment.get('commitments')
    if (not isinstance(commitments, dict) or set(commitments) != {'participant_activity', 'causal_relation'}
            or any(type(flag) is not bool for flag in commitments.values())):
        return {'status': 'unavailable', 'basis': basis, 'reason': 'empirical_commitments_invalid'}
    if basis == "unknown":
        return {"status": "unknown", "basis": basis, "reason": "support_not_established"}
    if basis == "not_applicable" and kind != "discourse":
        pure_choice = (kind == 'judgment' and statement.get('meaning_kind') == 'plan'
            and statement.get('time_scope') != 'reference_date'
            and not any(statement.get(key) for key in ('bound_fact_refs', 'time_fact_refs',
                                                      'observation_dates', 'classifications'))
            and not any(commitments.values())
            and all(assessment['checks'][key] == 'not_applicable'
                    for key in ('identity_scope', 'time', 'quantity')))
        if not pure_choice:
            return {"status": "unknown", "basis": basis, "reason": "substantive_commitment_needs_support"}
    if (kind in {"actor_activity", "causal_relation"} or any(commitments.values())
            or any(c['property'] == 'participant_activity' for c in statement.get('classifications', []))) and basis != "direct":
        return {"status": "unknown", "basis": basis, "reason": "inference_is_not_actor_or_cause_evidence"}
    return {"status": "compatible", "basis": basis, "reason": ""}
