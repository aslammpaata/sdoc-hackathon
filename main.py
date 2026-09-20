# main.py
from dotenv import load_dotenv

load_dotenv()  # local dev reads .env; on Cloud Run the env comes from deploy.sh

from fastapi import FastAPI, HTTPException  # noqa: E402
import os  # noqa: E402

app = FastAPI()

@app.get("/")
def root():
    return {"status": "ok", "message": "SDOC hackathon Hello World!"}

@app.get("/health")
def health():
    return {"healthy": True}

@app.get("/debug/store")
def debug_store():
    """Round-trip a probe case through the real Firestore + GCS (local ADC check).

    Only reachable when DEBUG_ROUTES=1 - never set that on the deployed service.
    Cleans up its own probe case and object afterwards.
    """
    if os.environ.get("DEBUG_ROUTES") != "1":
        raise HTTPException(status_code=404, detail="Not Found")

    import uuid
    from app import store
    from app.ingest import get_inbox

    email_id = f"_probe_{uuid.uuid4().hex[:8]}"
    payload = b"probe attachment bytes"
    result = {"project": store.PROJECT_ID, "bucket": store.ATTACHMENTS_BUCKET, "email_id": email_id}

    store.upsert_case(email_id, category="BL_COMPARISON", status="NEEDS_REVIEW",
                      review_reason="unreadable", has_defect=False, defect_fields=[])
    result["get_case"] = store.get_case(email_id)
    result["in_review_queue"] = any(c["email_id"] == email_id for c in store.list_review_queue())
    result["attachment_uri"] = store.put_attachment(email_id, "probe.txt", payload)
    result["attachment_roundtrip"] = store.get_attachment(email_id, "probe.txt") == payload
    result["submission_entry"] = store.build_submission()[email_id]
    result["inbox"] = {"source": get_inbox().source, "email_count": len(get_inbox().emails())}

    store.delete_case(email_id)
    store.delete_attachment(email_id, "probe.txt")
    result["cleaned_up"] = store.get_case(email_id) is None
    return result
