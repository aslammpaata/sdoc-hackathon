# SDOC — Shipping Document Verification

Averis × Monash Hackathon 2026 entry. Reads a shipping-operations inbox, sorts
every email into one of five categories, and for document-check requests compares
the **Shipping Instruction (SI)** against the **draft Bill of Lading (BL)** field by
field, reporting exactly which fields differ. Cases the system cannot decide go to
a human review queue with the evidence laid out side by side.

**Live:** https://sdoc-api-he56zusm2a-as.a.run.app

| Path | What it is |
| --- | --- |
| `/review` | Review queue — every case that needs a human decision |
| `/review/{email_id}` | SI vs BL side by side, per-field evidence, correction form |
| `/api/submission` | The scored output (`submission.json`) projected live from the database |
| `/docs` | Interactive API reference |

## What it does

```
inbox ─▶ 1 ingest & clean ─▶ 2 classify ─▶ 3 validate & route ─▶ 4 extract ─▶ 5 normalise & compare ─▶ 6 decide ─▶ review queue / submission.json
```

1. **Ingest & clean** — strips quoted reply history and warning banners *before*
   anything downstream reads the email (stale quoted text steering a decision is a
   known trap), keeps the raw record for audit, inventories attachments and stores
   them in Cloud Storage.
2. **Classify** — one Gemini call over subject + cleaned body + attachment
   inventory. Never the subject alone; subjects in this data are frequently
   misleading. Any language.
3. **Validate & route** — file format from magic bytes, not the extension; readers
   for txt / PDF / DOCX / XLSX, and scanned pages go to Gemini's vision — no OCR
   engine to install. Business document type (SI, BL, something else) is decided
   from the content. Unusable cases exit here, before any extraction cost.
4. **Extract** — the 7 compared fields (shipper, consignee, notify party, port of
   loading, port of discharge, container count, gross weight) into a canonical
   schema, each with its verbatim source text, location, method and confidence.
5. **Normalise & compare** — deterministic, no LLM. Party names lose legal
   suffixes and addresses, ports resolve to UN/LOCODE (Chinese, Arabic and Malay
   spellings included — language alone never produces a mismatch), weights convert
   to kg. Each field gets a reason: matched / matched after normalisation /
   mismatched / unreadable / missing.
6. **Decide** — deterministic and idempotent. A confident mismatch beats partial
   uncertainty; escalation to a human happens only when the case is genuinely
   undecidable. A reviewer's decision, once saved, survives every re-run.

**Stack:** FastAPI on Cloud Run (single service, server-rendered Jinja UI, no
build step), Firestore (one `cases` collection — the review queue is a query over
it, and `submission.json` is a projection over it, so the report and the UI can't
disagree), Cloud Storage for attachments, Gemini 2.5 Flash on Vertex AI (no API
key anywhere — Application Default Credentials locally, the service account on
Cloud Run).

**Results on the 520-email evaluation set** (organizer scorer): classification
macro-F1 ≥ 0.996, defect F1 1.0, end-to-end 46/46, review-escalation precision
1.0. See `CLAUDE.md` for the full engineering log.

## Run it

- **Local, from a fresh laptop:** follow **[SETUP.md](SETUP.md)** — ten steps,
  two of which need a browser login you must do yourself.
- **Process the inbox:** `python main.py run` (stages 1–6, ~7 min for 520 emails)
  or `python main.py decide` (stage 6 only, seconds).
- **Score:** `python scripts/score.py --summary` against the organizer's `/submit`
  server. The ground-truth file shipped with the organizer bundle is never opened.
- **Deploy:** `./scripts/deploy.sh` — builds, pushes to Artifact Registry, deploys
  to the same Cloud Run URL, tagged by commit hash.

## Repository

```
main.py           FastAPI app, pipeline runner, review routes
app/ingest.py     stage 1     app/classify.py   stage 2     app/documents.py  stage 3
app/extract.py    stage 4     app/compare.py    stage 5     app/decide.py     stage 6
app/store.py      Firestore + Cloud Storage     app/llm_client.py  the one Gemini interface
app/schema.py     canonical fields, contract    templates/         review UI
scripts/          deploy.sh, score.py           data/loader.py     organizer inbox loader
```

## Scope

The system verifies that the SI and BL are **consistent** with each other. It does
not verify document **authenticity** — sender verification, malware scanning,
signatures, issuer validation and alteration detection belong to a separate module.
