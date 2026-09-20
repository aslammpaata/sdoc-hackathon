"""Firestore + Cloud Storage access. The only module that talks to GCP storage.

Cloud Run's disk is ephemeral, so every stage persists through here:
  - cases live in one Firestore collection keyed by email_id
  - attachments live in one GCS bucket, one folder per email_id
  - the review queue is a query over `cases`, not a second collection
  - submission.json is a projection over `cases`, so report and UI can't disagree

Clients are created on first use so importing this module needs no credentials.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Iterable

from google.cloud import firestore, storage
from google.cloud.firestore_v1.base_query import FieldFilter

from app.schema import (
    SUBMISSION_KEYS,
    Case,
    Status,
    Submission,
    SubmissionEntry,
    empty_submission_entry,
)

PROJECT_ID = os.environ.get("GCP_PROJECT", "sdoc-hackathon")
CASES_COLLECTION = os.environ.get("FIRESTORE_COLLECTION", "cases")
ATTACHMENTS_BUCKET = os.environ.get("GCS_BUCKET", "sdoc-hackathon-attachments")


@lru_cache(maxsize=1)
def _db() -> firestore.Client:
    return firestore.Client(project=PROJECT_ID)


@lru_cache(maxsize=1)
def _bucket() -> storage.Bucket:
    return storage.Client(project=PROJECT_ID).bucket(ATTACHMENTS_BUCKET)


def _cases() -> firestore.CollectionReference:
    return _db().collection(CASES_COLLECTION)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def upsert_case(email_id: str, **fields) -> Case:
    """Merge `fields` into cases/{email_id}, creating it if absent.

    Merge is deep: passing si={"shipper": {...}} updates only that sub-map and
    leaves other si fields in place. To clear a field, pass it explicitly as
    None. `email_id` and `updated_at` are always written.
    """
    doc = dict(fields)
    doc["email_id"] = email_id
    doc["updated_at"] = _now()
    _cases().document(email_id).set(doc, merge=True)
    return doc  # type: ignore[return-value]


def get_case(email_id: str) -> Case | None:
    snap = _cases().document(email_id).get()
    return snap.to_dict() if snap.exists else None  # type: ignore[return-value]


def list_cases() -> list[Case]:
    return [snap.to_dict() for snap in _cases().stream()]  # type: ignore[misc]


def list_review_queue() -> list[Case]:
    """Cases awaiting a human decision: status == NEEDS_REVIEW."""
    query = _cases().where(filter=FieldFilter("status", "==", str(Status.NEEDS_REVIEW)))
    return [snap.to_dict() for snap in query.stream()]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


def attachment_blob_name(email_id: str, filename: str) -> str:
    # basename only — never let a filename from an email traverse into another folder
    return f"{email_id}/{os.path.basename(filename)}"


def put_attachment(email_id: str, filename: str, data: bytes) -> str:
    """Upload raw attachment bytes and return its stable gs:// uri."""
    blob = _bucket().blob(attachment_blob_name(email_id, filename))
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    blob.upload_from_string(data, content_type=content_type)
    return f"gs://{ATTACHMENTS_BUCKET}/{blob.name}"


def get_attachment(email_id: str, filename: str) -> bytes:
    return _bucket().blob(attachment_blob_name(email_id, filename)).download_as_bytes()


def sha256(data: bytes) -> str:
    """File hash for the audit record, so evidence points at exact bytes."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Submission projection
# ---------------------------------------------------------------------------


def to_submission_entry(case: Case) -> SubmissionEntry:
    """Project one case onto the exact contract shape — nothing more, nothing less."""
    entry = empty_submission_entry()
    for key in SUBMISSION_KEYS:
        if key in case:
            entry[key] = case[key]  # type: ignore[literal-required]
    entry["has_defect"] = bool(entry["has_defect"])
    entry["defect_fields"] = list(entry["defect_fields"] or [])
    return entry


def build_submission(email_ids: Iterable[str] | None = None) -> Submission:
    """submission.json as a projection over all cases.

    Pass the inbox's email_ids to guarantee every one is present: any id with
    no case yet gets a default GENERAL entry rather than being silently absent
    (a missing key is scored as wrong for that email).
    """
    submission: Submission = {c["email_id"]: to_submission_entry(c) for c in list_cases()}
    for email_id in email_ids or ():
        submission.setdefault(email_id, empty_submission_entry())
    return dict(sorted(submission.items()))
