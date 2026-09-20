"""Stage 2 — classify each cleaned email into one of the five categories.

Subject, cleaned body and the attachment inventory go into ONE prompt: the
subject alone is never trusted (misleading subjects are a confirmed trap in the
dataset), and the model is told the content may be in any language.

All model calls go through app.llm_client — nothing here touches the Gemini SDK.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app import llm_client, store
from app.schema import Case, Category, Classification

log = logging.getLogger(__name__)

PROMPT_VERSION = "1.1"

CATEGORY_DEFINITIONS = {
    Category.BL_COMPARISON: (
        "Anything whose goal is to get a draft Bill of Lading (BL) checked against the Shipping "
        "Instruction (SI) for a specific shipment: 'please check/verify/confirm the BL matches the SI', "
        "'attached SI and draft BL for confirmation', AND requests like 'please send the draft BL for "
        "checking' or 'compare SI and draft BL (BL still missing)'. The documents do NOT have to be "
        "attached - a comparison request with a missing attachment is still BL_COMPARISON (a later "
        "stage flags the missing file)."
    ),
    Category.SI_REQUEST: (
        "A person supplying or asking for a NEW shipping instruction for a specific shipment: a customer "
        "sending SI details (shipper, consignee, POL/POD, cargo) so the SI/booking can be prepared, or "
        "asking for the SI for a named order/booking. Not bulk automated reminders (see GENERAL)."
    ),
    Category.INVOICE_QUERY: (
        "A question or request about an invoice or charges: invoice breakdown, local charges, THC, "
        "D&D / demurrage, billing disputes, cancel or reissue an invoice, payment/credit notes."
    ),
    Category.GENERAL: (
        "Any other legitimate operational message: status updates, vessel/schedule notices, process "
        "notifications, automated or bulk reminders (e.g. 'submit SI & AED for all pending shipments'), "
        "follow-ups, acknowledgements, internal notices that ask for no specific document check."
    ),
    Category.SPAM: (
        "Unsolicited or irrelevant mail: marketing, promotions, phishing, scams, storage/password "
        "warnings, investment offers, anything not part of shipping-documentation work."
    ),
}

SYSTEM_PROMPT = (
    "You classify emails for a shipping-documentation operations team into exactly one category.\n"
    "Rules:\n"
    "1. Decide from what the BODY actually asks for, together with the attachment inventory. "
    "The SUBJECT line is only a weak hint: subjects are often stale, reused across threads, or "
    "misleading, and must never decide the category on their own.\n"
    "2. The email may be written in any language (English, Chinese, Arabic, Malay, ...). "
    "Classify by meaning, not by language.\n"
    "3. Quoted history and banners have already been removed; what you see is the sender's own message.\n"
    "4. Pick the single best category. Give a confidence between 0 and 1 and a one-sentence reason "
    "that cites the evidence you used.\n\n"
    "Categories:\n" + "\n".join(f"- {c}: {d}" for c, d in CATEGORY_DEFINITIONS.items())
)

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "category": {"type": "STRING", "enum": [c.value for c in Category]},
        "confidence": {"type": "NUMBER"},
        "reason": {"type": "STRING"},
    },
    "required": ["category", "confidence", "reason"],
}


def build_prompt(case: Case) -> str:
    email = case["email"]
    inv = case.get("attachments") or []
    if inv:
        inventory = "\n".join(
            f"- {a['filename']} (type: {a['declared_type'] or 'unknown'}, {a['size_bytes']} bytes)" for a in inv
        )
    else:
        inventory = "- (no attachments)"
    return (
        f"From: {email['sender']}\n"
        f"Subject: {email['subject']}\n"
        f"Attachments ({len(inv)}):\n{inventory}\n\n"
        f"Body:\n\"\"\"\n{email['body']}\n\"\"\"\n\n"
        "Classify this email."
    )


def classify(case: Case) -> tuple[Category, Classification]:
    """Return (category, evidence) for a stage-1 case. Raises on model failure."""
    result = llm_client.generate_json(build_prompt(case), RESPONSE_SCHEMA, system=SYSTEM_PROMPT)
    category = Category(result["category"])  # ValueError if the model strayed off the enum
    evidence = Classification(
        confidence=float(result.get("confidence", 0.0)),
        reason=str(result.get("reason", "")),
        model=llm_client.MODEL,
        prompt_version=PROMPT_VERSION,
        error=None,
    )
    return category, evidence


def classify_case(case: Case, persist: bool = True) -> Case:
    """Stage 2 for one case: set category (+ evidence) and upsert to Firestore.

    Never raises: if the model fails after retries the case is recorded as GENERAL
    with classification.error set, so the pipeline finishes and the failure is
    visible in the case record rather than as a crash.
    """
    try:
        category, evidence = classify(case)
    except Exception as e:  # noqa: BLE001 — recorded, not swallowed
        log.exception("classify failed for %s", case["email_id"])
        category = Category.GENERAL
        evidence = Classification(
            confidence=0.0,
            reason="",
            model=llm_client.MODEL,
            prompt_version=PROMPT_VERSION,
            error=f"{type(e).__name__}: {e}"[:500],
        )
    case["category"] = category.value
    case["classification"] = evidence
    case.setdefault("audit", {})["classified_at"] = datetime.now(timezone.utc).isoformat()
    if persist:
        store.upsert_case(
            case["email_id"],
            category=case["category"],
            classification=evidence,
            audit={"classified_at": case["audit"]["classified_at"]},
        )
    return case
