"""Stage 1 — ingest & clean.

For each email: strip quoted reply history and warning banners BEFORE anything
downstream reads subject/body (a quoted old message steering classification is a
known trap), keep the untouched record for audit, inventory the attachments and
write their bytes to Cloud Storage, then persist the case with category=None for
stage 2 to fill in.

INBOX_SOURCE picks where emails come from; nothing else in the app should know:
  http://localhost:8080                          the organizer Docker server (local dev)
  gs://sdoc-hackathon-attachments/dataset        the uploaded static bundle (Cloud Run)
  /some/folder                                   an extracted static bundle on disk
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from functools import lru_cache

from google.cloud import storage

from app import store
from app.schema import AttachmentInfo, Case, CleanedEmail, RawEmail
from data.loader import Inbox

log = logging.getLogger(__name__)

INBOX_SOURCE = os.environ.get("INBOX_SOURCE", "http://localhost:8080")


class GcsInbox(Inbox):
    """loader.Inbox over a gs://bucket/prefix that mirrors the bundle layout
    (inbox/*.json, attachments/*, sample_submission.json)."""

    def __init__(self, source: str):
        super().__init__(source)
        bucket_name, _, prefix = self.source.removeprefix("gs://").partition("/")
        self._bucket = storage.Client().bucket(bucket_name)
        self._prefix = prefix.strip("/")

    def _blob(self, rel_path: str) -> storage.Blob:
        rel_path = rel_path.lstrip("/")
        return self._bucket.blob(f"{self._prefix}/{rel_path}" if self._prefix else rel_path)

    def emails(self):
        prefix = f"{self._prefix}/inbox/" if self._prefix else "inbox/"
        blobs = sorted(
            (b for b in self._bucket.list_blobs(prefix=prefix) if b.name.endswith(".json")),
            key=lambda b: b.name,
        )
        return [json.loads(b.download_as_text()) for b in blobs]

    def get(self, email_id):
        return json.loads(self._blob(f"inbox/{email_id}.json").download_as_text())

    def read_bytes(self, att_path):
        return self._blob(att_path).download_as_bytes()

    def sample_submission(self):
        return json.loads(self._blob("sample_submission.json").download_as_text())


@lru_cache(maxsize=1)
def get_inbox(source: str | None = None) -> Inbox:
    """The one place an Inbox is built. Cached: the client and source don't change."""
    source = source or INBOX_SOURCE
    if source.startswith("gs://"):
        return GcsInbox(source)
    return Inbox(source)


# ---------------------------------------------------------------------------
# Cleaning — runs before any stage reads subject/body
# ---------------------------------------------------------------------------

CLEANER_VERSION = "1.0"

# A line matching any of these starts quoted history: everything from it onward is cut.
_QUOTE_START = re.compile(
    r"^(?:"
    r"_{5,}\s*$"                                   # Outlook separator rule
    r"|-{2,}\s*Original Message\s*-{2,}"           # "-----Original Message-----"
    r"|-{2,}\s*Forwarded message\s*-{2,}"          # Gmail forward
    r"|Begin forwarded message:"                   # Apple Mail forward
    r"|On .{3,120}?wrote:\s*$"                     # "On <date>, <name> wrote:"
    r"|From:\s.+\n(?:Sent|Date):\s"                # bare Outlook header block
    r")",
    re.MULTILINE | re.IGNORECASE,
)
# Whole lines to delete wherever they appear.
_BANNER_LINE = re.compile(
    r"^[ \t]*(?:"
    r"WARNING: This email originated (?:from )?outside(?: of)? (?:our|the) organi[sz]ation\b.*"
    r"|CAUTION:.*\b(?:external|outside)\b.*"
    r"|\[?(?:EXTERNAL|EXT)\]?[ :-]+(?:This|Email|Message).*"
    r"|This (?:e-?mail|message)(?: and any attachments)? (?:is|are) (?:confidential|intended solely).*"
    r")[ \t]*\n?",
    re.MULTILINE | re.IGNORECASE,
)
_QUOTED_LINE = re.compile(r"^[ \t]*>.*\n?", re.MULTILINE)
_SUBJECT_TAG = re.compile(r"^\s*\[(?:EXTERNAL|EXT)\]\s*", re.IGNORECASE)


def clean_body(body: str) -> tuple[str, dict[str, object]]:
    """Return (cleaned body, what-was-removed) — the audit record keeps the raw copy."""
    body = (body or "").replace("\r\n", "\n")
    info: dict[str, object] = {
        "cleaner_version": CLEANER_VERSION,
        "quoted_history_removed": False,
        "banner_lines_removed": 0,
        "quoted_lines_removed": 0,
    }
    m = _QUOTE_START.search(body)
    if m:
        body = body[: m.start()]
        info["quoted_history_removed"] = True
    body, n = _BANNER_LINE.subn("", body)
    info["banner_lines_removed"] = n
    body, n = _QUOTED_LINE.subn("", body)
    info["quoted_lines_removed"] = n
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body, info


def clean_subject(subject: str) -> str:
    return re.sub(r"\s+", " ", _SUBJECT_TAG.sub("", subject or "")).strip()


def clean_email(raw: RawEmail) -> CleanedEmail:
    body, info = clean_body(raw["body"])
    info["chars_removed"] = len(raw["body"]) - len(body)
    return CleanedEmail(subject=clean_subject(raw["subject"]), body=body, sender=raw["sender"], cleaning=info)


# ---------------------------------------------------------------------------
# Attachments — inventory + bytes to GCS. Content is NOT read here (stage 3).
# ---------------------------------------------------------------------------


def inventory_attachments(email_id: str, paths: list[str], inbox: Inbox) -> list[AttachmentInfo]:
    items: list[AttachmentInfo] = []
    for path in paths:
        data = inbox.read_bytes(path)
        filename = os.path.basename(path)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        uri = store.put_attachment(email_id, filename, data)
        items.append(
            AttachmentInfo(
                filename=filename,
                path=path,
                declared_type=ext,
                size_bytes=len(data),
                sha256=store.sha256(data),
                gcs_uri=uri,
            )
        )
    return items


# ---------------------------------------------------------------------------
# Stage entrypoints
# ---------------------------------------------------------------------------


def to_raw_email(email: dict) -> RawEmail:
    return RawEmail(
        email_id=email["email_id"],
        subject=email.get("subject", ""),
        body=email.get("body", ""),
        attachments=list(email.get("attachments") or []),
        sender=email.get("from", ""),
    )


def ingest_email(email: dict, inbox: Inbox | None = None, persist: bool = True) -> Case:
    """Stage 1 for one inbox record. Returns the case; upserts it when persist=True."""
    inbox = inbox or get_inbox()
    raw = to_raw_email(email)
    cleaned = clean_email(raw)
    attachments = inventory_attachments(raw["email_id"], raw["attachments"], inbox)
    case = Case(
        email_id=raw["email_id"],
        email=cleaned,
        raw_email=raw,
        attachments=attachments,
        category=None,  # stage 2 fills this in
        audit={
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "file_hashes": {a["filename"]: a["sha256"] for a in attachments},
        },
    )
    if persist:
        store.upsert_case(raw["email_id"], **{k: v for k, v in case.items() if k != "email_id"})
    return case


def ingest_all(inbox: Inbox | None = None, persist: bool = True):
    """Yield a stage-1 case for every email in the inbox."""
    inbox = inbox or get_inbox()
    for email in inbox.emails():
        yield ingest_email(email, inbox, persist=persist)
