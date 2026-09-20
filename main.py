# main.py
from dotenv import load_dotenv

load_dotenv()  # local dev reads .env; on Cloud Run the env comes from deploy.sh

from fastapi import FastAPI, Form, HTTPException, Request  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402
import os  # noqa: E402

app = FastAPI(title="SDOC — SI vs BL verification")
templates = Jinja2Templates(directory="templates")

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
            fields["audit"]["stage3_exit"] = str(routed["review_reason"])  # survives stage 6's rewrite
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
    from app.decide import decide_case
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
        # Every other category skips stages 3-5; stage 6 still runs so status is explicit.
        if case.get("category") == Category.BL_COMPARISON:
            case = analyse_case(case)
        return decide_case(case)

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
        "statuses": dict(Counter(str(c.get("status")) for c in cases)),
        "review_reasons": dict(Counter(c["review_reason"] for c in cases if c.get("status") == "NEEDS_REVIEW")),
        "classification_errors": [c["email_id"] for c in cases if c.get("classification", {}).get("error")],
        "analysis_errors": [c["email_id"] for c in cases if (c.get("audit") or {}).get("error")],
    }


def run_decide(force: bool = False) -> dict:
    """Stage 6 only, over every case already in Firestore. No LLM calls — seconds.
    Human corrections from the review UI are kept unless force=True."""
    from collections import Counter

    from app import store
    from app.decide import decide_case, human_decided

    stored = store.list_cases()
    kept = 0 if force else sum(1 for c in stored if human_decided(c))
    cases = [decide_case(c, force=force) for c in stored]
    return {
        "decided": len(cases),
        "human_decisions_kept": kept,
        "statuses": dict(Counter(str(c.get("status")) for c in cases)),
        "review_reasons": dict(Counter(c["review_reason"] for c in cases if c.get("status") == "NEEDS_REVIEW")),
        "no_documents_to_compare": sum(
            1 for c in cases if (c.get("audit") or {}).get("decision_note") == "no_documents_to_compare"
        ),
    }


@app.post("/api/run")
def api_run(limit: int | None = None, workers: int = 4):
    """Run stages 1-6 over the whole inbox. Synchronous: ~7 min for 520 emails."""
    return run_pipeline(limit=limit, workers=workers)


@app.post("/api/decide")
def api_decide(force: bool = False):
    """Re-run stage 6 only (no LLM) over the cases already in Firestore.
    force=true discards reviewer corrections and re-applies the automatic rule."""
    return run_decide(force=force)


# ---------------------------------------------------------------------------
# Review queue UI — a consumer of stage 6, server-rendered, no prefix
# ---------------------------------------------------------------------------


@app.get("/review")
def review_queue(request: Request):
    from app import store

    cases = sorted(store.list_review_queue(), key=lambda c: c["email_id"])
    return templates.TemplateResponse(request, "review_list.html", {"cases": cases})


@app.get("/review/{email_id}")
def review_detail(request: Request, email_id: str):
    from app import store
    from app.schema import COMPARED_FIELDS

    case = store.get_case(email_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"no case {email_id}")
    return templates.TemplateResponse(request, "review_detail.html", {"case": case, "fields": COMPARED_FIELDS})


@app.post("/review/{email_id}")
def review_submit(
    email_id: str,
    status: str = Form(...),
    defect_fields: list[str] = Form([]),
    resolved_by: str = Form(...),
    note: str = Form(""),
):
    from app import store
    from app.decide import apply_correction

    case = store.get_case(email_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"no case {email_id}")
    try:
        apply_correction(case, status, defect_fields, resolved_by, note)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return RedirectResponse(url="/review", status_code=303)


@app.get("/api/submission")
def api_submission():
    """submission.json: every id in sample_submission.json, projected from Firestore."""
    from app import store
    from app.ingest import get_inbox

    return store.build_submission(get_inbox().sample_submission().keys())


if __name__ == "__main__":
    # `python main.py run [limit]` — stages 1-6 locally; `decide` — stage 6 only, no LLM
    import json
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "run":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
        print(json.dumps(run_pipeline(limit=limit), indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "decide":
        print(json.dumps(run_decide(force="--force" in sys.argv), indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "retry-errors":
        # re-run only the cases whose classification failed (e.g. after a 429 burst)
        from app import store

        failed = [c["email_id"] for c in store.list_cases() if c.get("classification", {}).get("error")]
        print(f"retrying {len(failed)} cases: {failed}")
        print(json.dumps(run_pipeline(email_ids=failed), indent=2) if failed else "nothing to retry")
    else:
        print("usage: python main.py run [limit] | decide [--force] | retry-errors")
