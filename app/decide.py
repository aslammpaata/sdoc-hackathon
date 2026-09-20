"""Stage 6 — decide, evidence & audit. No LLM: every outcome here is reproducible.

Turns the per-field reason codes from stage 5 (or the stage-3 exit reason when a
case never reached comparison) into the submission contract — status,
review_reason, has_defect, defect_fields — and persists it on the case together
with an audit record. The review queue is a query over the same documents
(status == NEEDS_REVIEW), and apply_correction() is how the review UI writes a
human decision back.

Two rules decide most cases:
  - A confident mismatch beats partial uncertainty. If one field clearly differs
    and another is unreadable, the answer is MISMATCH, not NEEDS_REVIEW.
  - Escalate only when genuinely undecidable. NEEDS_REVIEW is scored on its own
    reliability axis, and escalating a case we could have decided loses twice.
    In particular, "please send me the draft BL" is a request, not a comparison
    with a missing file: nothing was ever attached, so there is nothing to decide.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from app import store
from app.compare import RULE_VERSION
from app.schema import (
    COMPARED_FIELDS,
    DEFECT_REASONS,
    Case,
    Category,
    Reason,
    ReviewReason,
    Status,
)

DECISION_VERSION = "1.0"

# The sender believed documents were attached: "attached", "enclosed", "dropped",
# "herewith". Without one of these, a zero-attachment email is a request (or the
# document is pasted in the body, e.g. "please find shipping instruction for X:").
_EXPECTS_ATTACHMENTS = re.compile(r"attach|enclos|dropped|herewith", re.I)

_CONTRACT_KEYS = ("status", "review_reason", "has_defect", "defect_fields")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_order(fields) -> list[str]:
    """Only the 7 canonical fields, in schema order — never an internal name."""
    wanted = set(fields)
    return [f for f in COMPARED_FIELDS if f in wanted]


def _decision(status, review_reason=None, defect_fields=(), note=None, undecided=()) -> dict:
    fields = _canonical_order(defect_fields)
    return {
        "status": None if status is None else str(status),
        "review_reason": None if review_reason is None else str(review_reason),
        "has_defect": bool(fields),
        "defect_fields": fields,
        "review": {"needed": status == Status.NEEDS_REVIEW},
        "audit": {
            "decided_at": _now(),
            "decision_version": DECISION_VERSION,
            "rule_version": RULE_VERSION,
            "decision_note": note,
            "undecided_fields": _canonical_order(undecided),
        },
    }


def stage3_exit(case: Case) -> str | None:
    """The stage-3 exit reason, from the audit record if stage 6 already ran (it
    rewrites the contract field), else from the contract field stage 3 wrote."""
    return (case.get("audit") or {}).get("stage3_exit") or (case.get("review_reason") if not case.get("comparison") else None)


def decide(case: Case) -> dict:
    """Pure: the contract fields + review/audit deltas for one case. Never raises.
    Idempotent: re-deciding an already-decided case gives the same answer."""
    if case.get("category") != Category.BL_COMPARISON:
        return _decision(None)

    attachments = case.get("attachments") or []
    comparison = case.get("comparison") or {}

    if not comparison:
        body = (case.get("email") or {}).get("body", "")
        if not attachments and not _EXPECTS_ATTACHMENTS.search(body):
            # A request for the draft BL (or an SI pasted in the body): nothing was
            # ever attached, so there is no comparison to decide.
            return _decision(None, note="no_documents_to_compare")
        exit_reason = stage3_exit(case)
        if exit_reason:
            return _decision(Status.NEEDS_REVIEW, exit_reason)
        if len(attachments) < 2:
            return _decision(Status.NEEDS_REVIEW, ReviewReason.MISSING_ATTACHMENT)
        # Stages 3-5 failed outright (audit.error): undecidable, and visible in the queue.
        return _decision(Status.NEEDS_REVIEW, ReviewReason.UNREADABLE, note="no_comparison_result")

    mismatched = [f for f, r in comparison.items() if r in DEFECT_REASONS]
    unreadable = [f for f, r in comparison.items() if r == Reason.UNREADABLE]
    missing = [f for f, r in comparison.items() if r == Reason.MISSING]
    undecided = unreadable + missing

    if mismatched:
        return _decision(Status.MISMATCH, defect_fields=mismatched, undecided=undecided)
    if unreadable:
        return _decision(Status.NEEDS_REVIEW, ReviewReason.UNREADABLE, undecided=undecided)
    if missing:
        return _decision(Status.NEEDS_REVIEW, ReviewReason.MISSING_VALUE, undecided=undecided)
    return _decision(Status.OK)


def human_decided(case: Case) -> bool:
    """True once a reviewer has saved a decision through the review UI."""
    return bool((case.get("review") or {}).get("resolved_by"))


def decide_case(case: Case, persist: bool = True, force: bool = False) -> Case:
    """Stage 6 for one case: apply decide() to the in-memory case and upsert it.

    A human decision recorded by apply_correction() wins over the automatic rule
    on every re-run unless force=True — re-running the pipeline must never quietly
    undo what a reviewer confirmed.
    """
    if human_decided(case) and not force:
        return case
    result = decide(case)
    exit_reason = stage3_exit(case)
    if exit_reason:  # keep it where the next decide can see it, even after we null review_reason
        result["audit"]["stage3_exit"] = str(exit_reason)
    for key in _CONTRACT_KEYS:
        case[key] = result[key]  # type: ignore[literal-required]
    case.setdefault("review", {}).update(result["review"])  # type: ignore[arg-type]
    case.setdefault("audit", {}).update(result["audit"])  # type: ignore[arg-type]
    if force and human_decided(case):
        result["review"] = {"needed": result["review"]["needed"], "resolved_by": None, "note": None,
                            "resolved_at": None, "corrections": {}}
        case["review"] = dict(result["review"])
    if persist:
        store.upsert_case(case["email_id"], **result)
    return case


# ---------------------------------------------------------------------------
# Review UI write-back
# ---------------------------------------------------------------------------

_CORRECTABLE = {Status.OK, Status.MISMATCH, Status.NEEDS_REVIEW}


def apply_correction(
    case: Case,
    status: str,
    defect_fields: list[str],
    resolved_by: str,
    note: str = "",
    review_reason: str | None = None,
    persist: bool = True,
) -> dict:
    """A human decision from the review UI. Validates, records what changed, upserts.

    Raises ValueError on a bad status/field so the route can return 400 instead of
    writing junk into the contract fields.
    """
    try:
        new_status = Status(status)
    except ValueError as e:
        raise ValueError(f"status must be one of {[s.value for s in _CORRECTABLE]}") from e
    bad = set(defect_fields) - set(COMPARED_FIELDS)
    if bad:
        raise ValueError(f"unknown defect fields: {sorted(bad)}")
    if new_status == Status.NEEDS_REVIEW:
        reason = ReviewReason(review_reason) if review_reason else ReviewReason.MISSING_VALUE
    else:
        reason = None
    fields = _canonical_order(defect_fields) if new_status == Status.MISMATCH else []

    update = {
        "status": new_status.value,
        "review_reason": None if reason is None else reason.value,
        "has_defect": bool(fields),
        "defect_fields": fields,
        "review": {
            "needed": new_status == Status.NEEDS_REVIEW,
            "resolved_by": resolved_by.strip() or "reviewer",
            "note": note.strip() or None,
            "resolved_at": _now(),
            "corrections": {
                "previous": {k: case.get(k) for k in _CONTRACT_KEYS},
                "status": new_status.value,
                "defect_fields": fields,
            },
        },
    }
    if persist:
        store.upsert_case(case["email_id"], **update)
    return update
