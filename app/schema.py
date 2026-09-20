"""Canonical types shared by every pipeline stage.

Nothing in here does work — it only names the shapes the stages pass around
and the exact output contract the scorer expects. If you change a value here,
you change it for every stage at once, which is the point.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Iterable, Literal, Mapping, TypedDict

# ---------------------------------------------------------------------------
# Compared fields — SI is the source of truth, BL is checked against it.
# ---------------------------------------------------------------------------

FieldName = Literal[
    "shipper",
    "consignee",
    "notify_party",
    "port_of_loading",
    "port_of_discharge",
    "container_count",
    "gross_weight_kg",
]

COMPARED_FIELDS: tuple[FieldName, ...] = (
    "shipper",
    "consignee",
    "notify_party",
    "port_of_loading",
    "port_of_discharge",
    "container_count",
    "gross_weight_kg",
)

# ---------------------------------------------------------------------------
# Enumerations that appear in the submission contract.
# StrEnum so they serialise to plain strings and compare equal to them.
# ---------------------------------------------------------------------------


class Category(StrEnum):
    BL_COMPARISON = "BL_COMPARISON"
    SI_REQUEST = "SI_REQUEST"
    INVOICE_QUERY = "INVOICE_QUERY"
    GENERAL = "GENERAL"
    SPAM = "SPAM"


class Status(StrEnum):
    OK = "OK"
    MISMATCH = "MISMATCH"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ReviewReason(StrEnum):
    WRONG_DOC_TYPE = "wrong_doc_type"
    MISSING_ATTACHMENT = "missing_attachment"
    UNREADABLE = "unreadable"
    MISSING_VALUE = "missing_value"


class DocType(StrEnum):
    """Business document type, verified by content in stage 3."""

    SI = "SI"
    BL = "BL"
    OTHER = "OTHER"


class ExtractionMethod(StrEnum):
    """How a field value was obtained — recorded for the audit trail."""

    TXT = "txt"
    PDF_TEXT = "pdf_text"
    DOCX = "docx"
    XLSX = "xlsx"
    OCR = "ocr"
    LLM = "llm"
    MANUAL = "manual"  # corrected by a reviewer in the review UI


class Reason(StrEnum):
    """Per-field comparison outcome (stage 5)."""

    MATCHED = "matched"
    MATCHED_AFTER_NORMALISATION = "matched_after_normalisation"
    MISMATCHED = "mismatched"
    UNREADABLE = "unreadable"
    MISSING = "missing"


# Reasons that count as a defect vs. those that mean "could not decide".
DEFECT_REASONS: frozenset[Reason] = frozenset({Reason.MISMATCHED})
UNDECIDED_REASONS: frozenset[Reason] = frozenset({Reason.UNREADABLE, Reason.MISSING})

# ---------------------------------------------------------------------------
# Extraction record — what stage 4 produces per field, per document.
# ---------------------------------------------------------------------------


class Extraction(TypedDict):
    value: str | int | float | None  # canonical value (e.g. UN/LOCODE, kg as float)
    original_text: str | None  # verbatim text as it appeared in the document
    source: str | None  # where it came from: "si.pdf:p1", "bl.xlsx:Sheet1!B4"
    method: ExtractionMethod | str | None
    confidence: float | None  # 0.0–1.0


ExtractedDocument = dict[FieldName, Extraction]
"""One document's fields, keyed by canonical field name."""

Comparison = dict[FieldName, Reason]
"""Stage 5 output: a reason code per compared field."""


def empty_extraction() -> Extraction:
    return Extraction(value=None, original_text=None, source=None, method=None, confidence=None)


# ---------------------------------------------------------------------------
# Submission contract — must match sample_submission.json exactly.
# ---------------------------------------------------------------------------


class SubmissionEntry(TypedDict):
    category: Category | str
    status: Status | str | None
    review_reason: ReviewReason | str | None
    has_defect: bool
    defect_fields: list[FieldName | str]


Submission = dict[str, SubmissionEntry]
"""submission[email_id] -> SubmissionEntry. Every email_id must be present."""

SUBMISSION_KEYS: tuple[str, ...] = (
    "category",
    "status",
    "review_reason",
    "has_defect",
    "defect_fields",
)


def empty_submission_entry(category: Category | str = Category.GENERAL) -> SubmissionEntry:
    """The shape for a non-comparison email (status/review_reason are null)."""
    return SubmissionEntry(
        category=str(category),
        status=None,
        review_reason=None,
        has_defect=False,
        defect_fields=[],
    )


def to_submission_entry(case: Mapping | None) -> SubmissionEntry:
    """Project one case onto the exact contract shape - nothing more, nothing less.

    Tolerates a case at any stage: missing keys fall back to the stage-2 defaults
    (status/review_reason null, has_defect false, defect_fields []). A case with no
    category yet (stage 1 only) is reported as GENERAL rather than omitted.
    """
    entry = empty_submission_entry()
    if not case:
        return entry
    for key in SUBMISSION_KEYS:
        if case.get(key) is not None:
            entry[key] = case[key]  # type: ignore[literal-required]
    entry["category"] = str(entry["category"])
    entry["status"] = None if entry["status"] is None else str(entry["status"])
    entry["review_reason"] = None if entry["review_reason"] is None else str(entry["review_reason"])
    entry["has_defect"] = bool(entry["has_defect"])
    entry["defect_fields"] = [str(f) for f in (entry["defect_fields"] or [])]
    return entry


def build_submission(cases: Mapping[str, Mapping] | Iterable[Mapping], email_ids: Iterable[str]) -> Submission:
    """submission.json for every id in `email_ids` (the sample_submission keys).

    Pure: takes cases already loaded (dict keyed by email_id, or an iterable of
    case dicts) and never crashes or drops an id - an email with no case at all
    gets the default GENERAL entry. Sorted by email_id for stable diffs.
    """
    by_id: Mapping[str, Mapping]
    if isinstance(cases, Mapping):
        by_id = cases
    else:
        by_id = {c["email_id"]: c for c in cases}
    return {eid: to_submission_entry(by_id.get(eid)) for eid in sorted(email_ids)}


# ---------------------------------------------------------------------------
# Firestore case document — the single record every stage reads and writes.
# ---------------------------------------------------------------------------


class RawEmail(TypedDict):
    """The email record exactly as the inbox served it (audit copy, never edited)."""

    email_id: str
    subject: str
    body: str
    attachments: list[str]  # paths as given, e.g. "attachments/email_004_SI.txt"
    sender: str  # the inbox's "from" key, renamed because `from` is reserved


class CleanedEmail(TypedDict):
    """What stages 2+ read: quoted history and banners already stripped."""

    subject: str
    body: str
    sender: str
    cleaning: dict[str, object]  # what was removed and why, for audit


class AttachmentInfo(TypedDict):
    """Stage-1 inventory entry: metadata only, content is stage 3's job."""

    filename: str
    path: str  # as given by the inbox
    declared_type: str  # extension, lower-case, no dot ("txt", "pdf", ...)
    size_bytes: int
    sha256: str
    gcs_uri: str


class Classification(TypedDict, total=False):
    """Stage-2 evidence for the category decision."""

    confidence: float
    reason: str
    model: str
    prompt_version: str
    error: str | None


class Audit(TypedDict, total=False):
    ingested_at: str
    file_hashes: dict[str, str]  # filename -> sha256
    readers_used: dict[str, str]  # filename -> ExtractionMethod
    rule_version: str
    processed_at: str  # ISO-8601 UTC


class Review(TypedDict, total=False):
    needed: bool
    resolved_by: str | None
    corrections: dict[str, object]
    resolved_at: str | None


class Case(TypedDict, total=False):
    email_id: str
    email: CleanedEmail
    raw_email: RawEmail
    attachments: list[AttachmentInfo]
    category: Category | str | None  # None until stage 2 has run
    classification: Classification
    status: Status | str | None
    review_reason: ReviewReason | str | None
    has_defect: bool
    defect_fields: list[str]
    si: ExtractedDocument
    bl: ExtractedDocument
    comparison: Comparison
    audit: Audit
    review: Review
