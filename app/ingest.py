"""Stage 1 — ingest. This file currently holds only the inbox connection.

INBOX_SOURCE picks where emails come from; nothing else in the app should know:
  http://localhost:8080                          the organizer Docker server (local dev)
  gs://sdoc-hackathon-attachments/dataset        the uploaded static bundle (Cloud Run)
  /some/folder                                   an extracted static bundle on disk
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

from google.cloud import storage

from data.loader import Inbox

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
