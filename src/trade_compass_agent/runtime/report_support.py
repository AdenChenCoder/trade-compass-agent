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
    if basis == "unknown":
        return {"status": "unknown", "basis": basis, "reason": "support_not_established"}
    if basis == "not_applicable" and kind != "discourse":
        return {"status": "unknown", "basis": basis, "reason": "substantive_commitment_needs_support"}
    if kind in {"actor_activity", "causal_relation"} and basis != "direct":
        return {"status": "unknown", "basis": basis, "reason": "inference_is_not_actor_or_cause_evidence"}
    return {"status": "compatible", "basis": basis, "reason": ""}
