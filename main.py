# main.py
from dotenv import load_dotenv

load_dotenv()  # local dev reads .env; on Cloud Run the env comes from deploy.sh

from fastapi import FastAPI, Form, HTTPException, Request  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

app = FastAPI(title="SDOC — SI vs BL verification")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

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
# UI — read-only views over the case records, server-rendered, no prefix.
# Only POST /review/{id} writes (a human decision); nothing here re-runs anything.
# ---------------------------------------------------------------------------

_CACHE_TTL_S = 60.0
_cache: dict = {"at": 0.0, "cases": None, "refreshing": False}
_cache_lock = threading.Lock()


def _refresh_cases() -> None:
    from app import store

    try:
        cases = sorted(store.list_cases(), key=lambda c: c["email_id"])
        with _cache_lock:
            _cache["cases"], _cache["at"] = cases, time.monotonic()
    finally:
        with _cache_lock:
            _cache["refreshing"] = False


def _cases_cached() -> tuple[list[dict], int]:
    """All cases, refreshed at most every 60 s: 520 Firestore reads per page load
    would be visibly slow. Stale-while-revalidate: a stale copy is served at once
    and refreshed in the background; only the very first request ever blocks.
    The review queue never uses this — it must be live."""
    with _cache_lock:
        empty = _cache["cases"] is None
        age = time.monotonic() - _cache["at"]
        stale = age > _CACHE_TTL_S
        if not empty and stale and not _cache["refreshing"]:
            _cache["refreshing"] = True
            threading.Thread(target=_refresh_cases, daemon=True).start()
    if empty:
        with _cache_lock:
            if _cache["refreshing"]:
                empty = False  # warm-up already running; fall through and wait below
            else:
                _cache["refreshing"] = True
        if not empty:
            for _ in range(600):  # wait for the in-flight warm-up rather than issue a second read
                time.sleep(0.1)
                with _cache_lock:
                    if _cache["cases"] is not None:
                        break
        else:
            _refresh_cases()
    with _cache_lock:
        return _cache["cases"] or [], int(time.monotonic() - _cache["at"])


@app.on_event("startup")
def _warm_cache() -> None:
    """Fill the case cache as the instance boots so the first visitor isn't the one paying for it."""
    with _cache_lock:
        _cache["refreshing"] = True
    threading.Thread(target=_refresh_cases, daemon=True).start()


READER_LABELS = {
    "txt": "plain text", "pdf_text": "PDF text layer", "docx": "Word (docx)", "xlsx": "Excel (xlsx)",
    "ocr": "scan → Gemini vision", "unreadable": "unreadable file",
}
DECISION_NOTES = {
    "no_documents_to_compare": "No documents to compare — a request for the draft BL, not a check",
    "no_comparison_result": "Stages 3–5 produced no comparison",
}

# A tour of this sample dataset. Ids absent from the loaded data are skipped.
TOUR = [
    ("email_501", "Invoice posing as a BL", "The 'BL' file is a commercial invoice — caught by reading the content, not the filename."),
    ("email_348", "Chinese label, composite port, party aliases", "毛重 (gross weight) label mapped to the canonical field; RUGAO/NANTONG/SHANGHAI resolved to one UN/LOCODE; shipper and consignee matched after stripping 'on behalf of' and addresses."),
    ("email_013", "Port swapped, code kept", "BL says TUTICORIN, INDIA (KEMBA) where the SI says MOMBASA, KENYA (KEMBA) — the bracketed code was left behind; the name decides, so the defect is caught."),
    ("email_517", "Placeholders instead of ports", "SI carries TBA and ____MT where both ports should be — undecidable, escalated as unreadable."),
    ("email_507", "One document of two", "A comparison was requested but only the SI arrived — escalated as missing_attachment."),
    ("email_512", "Scanned SI, read by vision", "No text layer; Gemini reads the page image directly — no OCR engine involved."),
    ("email_513", "Scanned document, vision + normalisation", "Read by vision, then the shipper matched only after normalisation."),
    ("email_514", "Scanned document, clean match", "Vision-read scan where every field agreed."),
]


def overview(cases: list[dict]) -> dict:
    from collections import Counter

    statuses = Counter(str(c.get("status")) if c.get("status") else "null" for c in cases)
    categories = Counter(c.get("category") or "?" for c in cases)
    readers = Counter(v for c in cases for v in ((c.get("audit") or {}).get("readers_used") or {}).values())
    reasons = Counter(c["review_reason"] for c in cases if c.get("status") == "NEEDS_REVIEW")
    defects = Counter(f for c in cases if c.get("status") == "MISMATCH" for f in c.get("defect_fields") or [])
    return {
        "processed": len(cases),
        "statuses": dict(statuses),
        "categories": categories.most_common(),
        "readers": readers.most_common(),
        "readers_total": max(sum(readers.values()), 1),
        "reader_labels": READER_LABELS,
        "reasons": reasons.most_common(),
        "defects": defects.most_common(),
        "defects_max": max(defects.values(), default=1),
    }


def build_trace(case: dict, ordered_ids: list[str] | None = None) -> dict:
    """The six-stage narrative for the detail page, derived only from stored fields."""
    from app.schema import COMPARED_FIELDS

    em, raw, audit = case.get("email") or {}, case.get("raw_email") or {}, case.get("audit") or {}
    cleaning = em.get("cleaning") or {}
    cls = case.get("classification") or {}
    comparison = case.get("comparison") or {}
    is_cmp = case.get("category") == "BL_COMPARISON"
    exit_reason = audit.get("stage3_exit")

    # Stage 3: which file was verified as SI / BL is implied by stage 4's sources.
    def role_of(filename: str) -> str | None:
        for side, label in (("si", "Shipping Instruction"), ("bl", "Bill of Lading")):
            for f in COMPARED_FIELDS:
                if filename in str(((case.get(side) or {}).get(f) or {}).get("source") or ""):
                    return label
        return None

    files = [
        {
            "filename": a["filename"], "declared": a.get("declared_type"), "size": a.get("size_bytes"),
            "read_as": (audit.get("readers_used") or {}).get(a["filename"], "—"),
            "read_label": READER_LABELS.get((audit.get("readers_used") or {}).get(a["filename"], ""), (audit.get("readers_used") or {}).get(a["filename"], "—")),
            "role": role_of(a["filename"]),
        }
        for a in case.get("attachments") or []
    ]
    no_docs = audit.get("decision_note") == "no_documents_to_compare"
    if not is_cmp:
        v_conclusion = ""
    elif comparison:
        v_conclusion = "Both documents readable and verified as an SI + BL pair — routed to extraction."
    elif no_docs:
        v_conclusion = "No documents attached and none claimed — a request for the draft BL, not a check. Nothing to validate."
    elif exit_reason == "missing_attachment":
        v_conclusion = f"Comparison requested but {len(files)} of 2 documents arrived — exited: missing_attachment."
    elif exit_reason == "wrong_doc_type":
        v_conclusion = "Both files readable, but their content is not an SI + BL pair — exited: wrong_doc_type."
    elif exit_reason == "unreadable":
        v_conclusion = "A file could not be read (corrupt or empty) — exited: unreadable."
    elif not files:
        v_conclusion = "No documents attached — nothing to validate; this is a request, not a check."
    else:
        v_conclusion = "No comparison result was produced." + (f" Error: {audit['error']}" if audit.get("error") else "")

    counts = {r: sum(1 for v in comparison.values() if v == r) for r in ("matched", "matched_after_normalisation", "mismatched", "unreadable", "missing")}
    if comparison:
        parts = [f"{counts['matched']} matched"]
        if counts["matched_after_normalisation"]:
            parts.append(f"{counts['matched_after_normalisation']} matched after normalisation")
        if counts["mismatched"]:
            parts.append(f"{counts['mismatched']} mismatched")
        if counts["unreadable"]:
            parts.append(f"{counts['unreadable']} unreadable")
        if counts["missing"]:
            parts.append(f"{counts['missing']} missing")
        c_conclusion = ", ".join(parts) + " of 7 fields."
    else:
        c_conclusion = ""

    status = case.get("status")
    if status == "OK":
        d_conclusion = "All seven fields agree (after normalisation where needed). No human needed."
    elif status == "MISMATCH":
        d_conclusion = f"{len(case.get('defect_fields') or [])} field(s) clearly differ from the SI — reported as MISMATCH" + (
            "; the unreadable/missing fields do not change that (a confident mismatch beats partial uncertainty)." if audit.get("undecided_fields") else ".")
    elif status == "NEEDS_REVIEW":
        d_conclusion = f"Genuinely undecidable ({case.get('review_reason')}) — escalated with the evidence above. NEEDS_REVIEW is scored separately, so this is used sparingly."
    elif is_cmp:
        d_conclusion = "No comparison applies: nothing was attached and nothing was claimed to be — status null, not an escalation."
    else:
        d_conclusion = f"{case.get('category')} is not a document-check request — status null."

    prev_id = next_id = None
    if ordered_ids and case.get("email_id") in ordered_ids:
        i = ordered_ids.index(case["email_id"])
        prev_id = ordered_ids[i - 1] if i > 0 else None
        next_id = ordered_ids[i + 1] if i + 1 < len(ordered_ids) else None

    return {
        "ingest": {
            "quoted_removed": bool(cleaning.get("quoted_history_removed")),
            "banner_lines": cleaning.get("banner_lines_removed", 0),
            "chars_removed": cleaning.get("chars_removed", 0),
            "raw_chars": len(raw.get("body") or ""), "clean_chars": len(em.get("body") or ""),
        },
        "classify": {k: cls.get(k) for k in ("confidence", "reason", "model", "prompt_version", "error")},
        "validate": {"ran": is_cmp, "exit": bool(exit_reason) and not comparison and not no_docs, "files": files, "conclusion": v_conclusion},
        "extract": {"ran": bool(case.get("si") or case.get("bl")),
                    "why": "the case exited at stage 3." if is_cmp else "only comparison requests are extracted.",
                    "conclusion": "Seven fields extracted from each document with verbatim source text, location and confidence."},
        "compare": {"ran": bool(comparison), "why": "there was nothing to compare.", "counts": counts, "conclusion": c_conclusion},
        "decide": {"note_label": DECISION_NOTES.get(audit.get("decision_note") or "", audit.get("decision_note") or ""), "conclusion": d_conclusion},
        "prev": prev_id, "next": next_id,
    }


@app.get("/", include_in_schema=False)
def dashboard(request: Request):
    cases, age = _cases_cached()
    present = {c["email_id"]: c for c in cases}
    tour = [
        {"email_id": eid, "title": title, "blurb": blurb, "status": present[eid].get("status")}
        for eid, title, blurb in TOUR if eid in present
    ]
    ov = overview(cases)
    ov["cached_seconds"] = age
    return templates.TemplateResponse(request, "dashboard.html", {"nav": "home", "overview": ov, "tour": tour})


@app.get("/cases", include_in_schema=False)
def cases_explorer(request: Request, status: str = "", category: str = "", reason: str = "", q: str = ""):
    from app.schema import Category, ReviewReason

    cases, _ = _cases_cached()
    q = q.strip()
    if q:
        if any(c["email_id"] == q for c in cases):
            return RedirectResponse(url=f"/cases/{q}", status_code=307)
    rows = cases
    if status:
        rows = [c for c in rows if (str(c.get("status")) if c.get("status") else "null") == status]
    if category:
        rows = [c for c in rows if c.get("category") == category]
    if reason:
        rows = [c for c in rows if c.get("review_reason") == reason]
    return templates.TemplateResponse(request, "cases.html", {
        "nav": "cases", "rows": rows, "total": len(cases),
        "f": {"status": status, "category": category, "reason": reason, "q": q},
        "categories": [c.value for c in Category], "reasons": [r.value for r in ReviewReason],
        "not_found": q or None,
    })


@app.get("/cases/{email_id}", include_in_schema=False)
def case_detail(request: Request, email_id: str):
    from app import store
    from app.schema import COMPARED_FIELDS

    case = store.get_case(email_id)  # live: a just-saved correction must show here
    if case is None:
        raise HTTPException(status_code=404, detail=f"no case {email_id}")
    cached, _ = _cases_cached()
    trace = build_trace(case, [c["email_id"] for c in cached])
    show_form = case.get("category") == "BL_COMPARISON" and case.get("status") is not None
    return templates.TemplateResponse(request, "case_detail.html", {
        "nav": "cases", "case": case, "fields": COMPARED_FIELDS, "trace": trace, "show_form": show_form,
    })


@app.get("/review")
def review_queue(request: Request):
    from app import store

    cases = sorted(store.list_review_queue(), key=lambda c: c["email_id"])  # never cached
    return templates.TemplateResponse(request, "review_list.html", {"nav": "review", "cases": cases})


@app.get("/review/{email_id}", include_in_schema=False)
def review_detail(email_id: str):
    """Old detail URL — the canonical page is /cases/{id}. POST /review/{id} is unchanged."""
    return RedirectResponse(url=f"/cases/{email_id}", status_code=307)


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
