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


# ---------------------------------------------------------------------------
# Pipeline: ingest -> classify -> (comparison cases only) validate -> extract -> compare
# ---------------------------------------------------------------------------


def analyse_case(case: dict, persist: bool = True) -> dict:
    """Stages 3-5 for one BL_COMPARISON case: validate & route, extract, compare.

    Never raises - a failure is recorded on the case so the run finishes. Writes only
    what stage 6 needs (si, bl, comparison, review_reason, audit) in a single upsert;
    status, has_defect and defect_fields are stage 6's call and are left absent.
    """
    import logging
    from datetime import datetime, timezone

    from app import compare, documents, extract, store

    fields: dict = {
        "review_reason": None,
        "audit": {
            "rule_version": compare.RULE_VERSION,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    try:
        routed = documents.validate_case(case)
        fields["audit"]["readers_used"] = routed["readers"]
        if routed.get("review_reason"):
            fields["review_reason"] = str(routed["review_reason"])  # stage 3 exit, no comparison
        else:
            si = extract.extract_document(**routed["si"])
            bl = extract.extract_document(**routed["bl"])
            fields["si"], fields["bl"] = si, bl
            fields["comparison"] = {f: str(r) for f, r in compare.compare(si, bl).items()}
    except Exception as e:  # noqa: BLE001 - recorded on the case, never fatal to the run
        logging.getLogger(__name__).exception("stages 3-5 failed for %s", case["email_id"])
        fields["audit"]["error"] = f"{type(e).__name__}: {e}"[:500]
    case.update(fields)
    if persist:
        store.upsert_case(case["email_id"], **fields)
    return case


def run_pipeline(limit: int | None = None, workers: int = 4, email_ids: list[str] | None = None) -> dict:
    """Run stages 1-5 over the inbox and persist each case. Returns a summary."""
    import logging
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor

    from app.classify import classify_case
    from app.ingest import get_inbox, ingest_email
    from app.schema import COMPARED_FIELDS, Category

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    inbox = get_inbox()
    emails = inbox.emails()
    if email_ids:
        wanted = set(email_ids)
        emails = [e for e in emails if e["email_id"] in wanted]
    if limit:
        emails = emails[:limit]

    def process(email: dict) -> dict:
        case = classify_case(ingest_email(email, inbox))
        # Every other category skips stages 3-5 and is left untouched for stage 6.
        return analyse_case(case) if case.get("category") == Category.BL_COMPARISON else case

    with ThreadPoolExecutor(max_workers=workers) as pool:
        cases = list(pool.map(process, emails))

    compared = [c for c in cases if c.get("comparison")]
    readers: Counter = Counter()
    for case in cases:
        readers.update((case.get("audit") or {}).get("readers_used", {}).values())
    return {
        "source": inbox.source,
        "processed": len(cases),
        "categories": dict(Counter(c["category"] for c in cases)),
        "readers_used": dict(readers),
        "stage3_exits": dict(Counter(c["review_reason"] for c in cases if c.get("review_reason"))),
        "compared": len(compared),
        "comparison_outcomes": dict(Counter(r for c in compared for r in c["comparison"].values())),
        "per_field": {f: dict(Counter(c["comparison"][f] for c in compared)) for f in COMPARED_FIELDS},
        "classification_errors": [c["email_id"] for c in cases if c.get("classification", {}).get("error")],
        "analysis_errors": [c["email_id"] for c in cases if (c.get("audit") or {}).get("error")],
    }


@app.post("/api/run")
def api_run(limit: int | None = None, workers: int = 4):
    """Run the pipeline over the whole inbox. Synchronous: ~2-3 min for 520 emails."""
    return run_pipeline(limit=limit, workers=workers)


@app.get("/api/submission")
def api_submission():
    """submission.json: every id in sample_submission.json, projected from Firestore."""
    from app import store
    from app.ingest import get_inbox

    return store.build_submission(get_inbox().sample_submission().keys())


if __name__ == "__main__":
    # `python main.py run [limit]` — run stages 1-2 locally without starting the server
    import json
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "run":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
        print(json.dumps(run_pipeline(limit=limit), indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "retry-errors":
        # re-run only the cases whose classification failed (e.g. after a 429 burst)
        from app import store

        failed = [c["email_id"] for c in store.list_cases() if c.get("classification", {}).get("error")]
        print(f"retrying {len(failed)} cases: {failed}")
        print(json.dumps(run_pipeline(email_ids=failed), indent=2) if failed else "nothing to retry")
    else:
        print("usage: python main.py run [limit] | retry-errors")
