# SDOC Hackathon — Project Context

**Event:** Averis x Monash Hackathon 2026. Problem: shipping document verification
(email inbox → classify → compare SI vs BL → discrepancy report).

## Timeline
- **Prelim submission: Tue 22 Sep, 12:00 PM.** Five deliverables: project description,
  GitHub repo + README with setup instructions, live public deployment, slide deck
  (architecture / implementation / challenges / roadmap), demo video.
- Video length is disputed: rules doc says 5 min, event site says 3 min. Penalty is
  1 mark per 30s over. **Confirm with organizers.**
- Mon 21 Sep is packaging day — README, deck, video. Not building.
- Finals (if shortlisted) 26 Sep. Final submission must be an *extension* of the
  prelim entry, so don't build throwaway code.

## Scoring — what earns points
Prelim rubric (100): Working Core Prototype 25, System Design & Architecture 15,
Technology Integration 15, Technical Feasibility & Validation 15, Problem Statement
Understanding 10, Innovation & Solution Approach 10, Practical Value 10.
**65 points are "it works, the architecture is sound, you can prove it."**

Self-eval formula: 50% end-to-end + 30% Stage-1 macro-F1 + 20% Stage-3 defect-F1.
`NEEDS_REVIEW` handling is scored on a **separate reliability axis, outside that
formula** — escalating a case you could have decided forfeits points twice over.
Escalate only when genuinely undecidable.

Macro-F1 means rare categories count as much as common ones. Don't let the
classifier ignore small classes.

Two hard rules: AI must be a key component (no pure-regex solution); cloud infra
must be *meaningfully* integrated (named managed services doing real work) or
scores may be reduced significantly.

## Output contract (must match exactly)
```
submission[email_id] = {
  "category":      "BL_COMPARISON" | "SI_REQUEST" | "INVOICE_QUERY" | "GENERAL" | "SPAM",
  "status":        "OK" | "MISMATCH" | "NEEDS_REVIEW" | null,
  "review_reason": "wrong_doc_type" | "missing_attachment" | "unreadable" | "missing_value" | null,
  "has_defect":    bool,
  "defect_fields": [...]
}
```
Every `email_id` present. Match `sample_submission.json` exactly.

**7 compared fields:** shipper, consignee, notify_party, port_of_loading,
port_of_discharge, container_count, gross_weight_kg. SI is source of truth.
Labels differ between documents ("Port of Loading" vs "Load Port") — map to
canonical schema at extraction time, not at comparison time.

## Architecture — 6 stages, one FastAPI service
1. **Ingest & Clean** — pull email + attachments, strip quoted reply history and
   warning banners before anything downstream reads them, preserve raw record for
   audit, write attachments to Cloud Storage.
2. **Classify** — 5-way category from the cleaned record. Subject + body +
   attachment inventory together, never subject alone (misleading subjects are a
   known trap in the dataset).
3. **Validate & Route** — comparison requests only: confirm SI and BL both present,
   detect real file format (not the extension), verify business document type by
   content, select reader (TXT / PDF / DOCX / XLSX / OCR). Unusable cases exit here
   to review, before extraction cost is spent.
4. **Extract** — each field into canonical schema with original text, source
   location, extraction method, confidence.
5. **Normalize & Compare** — deterministic, no LLM. Party names, ports, container
   counts, weight units normalized before comparison. Reason code per field:
   matched / matched_after_normalisation / mismatched / unreadable / missing.
   **Most of the score lives here.**
6. **Decide, Evidence & Audit** — final status with side-by-side evidence, review
   queue entry when undecidable, persisted audit record (file hashes, extraction
   methods, rule version).

A confident mismatch beats partial uncertainty: if one field clearly differs and
another is unreadable, report MISMATCH, not NEEDS_REVIEW.

Review queue UI is a *consumer* of stage 6 output, not a seventh stage.

Keep all LLM calls behind one interface (`llm_client.py`) — swap point if rate-limited.

## Multilingual — confirmed present in the real dataset
Non-English documents are in the sample data (verified by inspection, not assumed).
This is **not** a separate pipeline stage — it's a requirement on stages 4 and 5.

- Extraction maps labels to canonical field names regardless of source language and
  keeps original text as evidence.
- Normalization resolves ports to UN/LOCODE so equivalents compare equal:
  `装货港: 上海，中国` → `port_of_loading = CNSHA` ← `Port of Loading: Shanghai, China`
- **Language alone must never produce a mismatch.**
- Port alias table and party-name normalizer need non-English entries; CJK text
  needs handling Latin-script rules won't cover.

## Storage & data model
**Cloud Run's filesystem is ephemeral** — anything written to local disk is lost
when the instance scales to zero or a new revision deploys. State must live outside
the container.

- **Firestore** (Native mode, asia-southeast1) — single `cases` collection keyed by
  `email_id`. Review queue is a query (`where status == "NEEDS_REVIEW"`), not a
  second collection. `submission.json` is a projection over the same collection, so
  report and UI can't disagree.
- **Cloud Storage** (`gs://sdoc-hackathon-attachments`) — attachment files, one
  folder per email_id. Needed so the review UI can show source documents and the
  audit record's file hashes point at stable objects.

```
cases/{email_id}
  category, status, review_reason, has_defect, defect_fields
  si: { shipper: {value, original_text, source, method, confidence}, ... }
  bl: { ... }
  comparison: { shipper: "matched", port_of_discharge: "mismatched", ... }
  audit: { file_hashes, readers_used, rule_version, processed_at }
  review: { needed, resolved_by, corrections, resolved_at }
```

## Frontend
**Server-rendered from the same FastAPI app** — Jinja2 templates, no build step, no
CORS, no second deploy target, one URL. A review queue is a list, a detail view and
a correction form; a form post does that without a framework.

```
GET  /review          → cases where status == NEEDS_REVIEW
GET  /review/{id}     → SI vs BL side by side, evidence, confidence
POST /review/{id}     → write correction to Firestore, redirect
GET  /api/submission  → submission.json projection
GET  /health
```

API stays under `/api/*` so it never collides with UI routes.

Exception: if the frontend owner is genuinely much faster in React, build a static
bundle and serve it from the same container via `StaticFiles`. Still one URL, still
no CORS. Do not split to Vercel — the cold-start risk it solves is better solved
with `--min-instances=1` before judging.

## Cloud setup — done, do not redo
- GCP project: `sdoc-hackathon` (project number 328117535233)
- Region: `asia-southeast1`
- Artifact Registry: `sdoc-repo`
- Cloud Run service: **`sdoc-api`**, live and publicly reachable (verified from a
  phone on cellular, not just localhost)
- Billing: Free Trial, $300 / 90 days. No auto-billing unless someone manually
  upgrades. This project should cost ~$0.
- Deploy: `./scripts/deploy.sh` from Git Bash. Builds, pushes, deploys, prints the
  URL. Tags images by git commit hash (`-dirty` suffix if uncommitted changes), so
  the running image always traces to exact code.
- **Deploying does not create a new URL.** Cloud Run reuses the same service and
  link; it swaps which code answers.
- Everyone deploys to the *same* service. Pull `main` before deploying, and say so
  in the group chat, or you'll overwrite a teammate's version.

### Firestore + Storage — done, do not redo
```bash
gcloud services enable firestore.googleapis.com storage.googleapis.com secretmanager.googleapis.com
gcloud firestore databases create --location=asia-southeast1
gcloud storage buckets create gs://sdoc-hackathon-attachments --location=asia-southeast1

# service account needs these or every call fails with PermissionDenied
gcloud projects add-iam-policy-binding sdoc-hackathon --member="serviceAccount:328117535233-compute@developer.gserviceaccount.com" --role="roles/datastore.user"
gcloud projects add-iam-policy-binding sdoc-hackathon --member="serviceAccount:328117535233-compute@developer.gserviceaccount.com" --role="roles/storage.objectAdmin"
```
Confirmed: `328117535233-compute@developer.gserviceaccount.com` already has
`roles/datastore.user`, `roles/storage.objectAdmin`, **and `roles/aiplatform.user`**
(added when Stage 2 classify.py went live — needed for the deployed service to call
Gemini via Vertex AI; see "LLM auth" below). Firestore database and the GCS bucket
both exist. Don't re-run these or report them as outstanding.

### Application Default Credentials (local dev only) — done on Aslam's machine
Confirmed working: `gcloud auth application-default login` +
`gcloud auth application-default set-quota-project sdoc-hackathon` run successfully,
and `/debug/store` round-tripped against the real Firestore project and GCS bucket
(not a mock) — verified, not just assumed.

**Still to do:** ADC is per-machine, not per-project-membership. **Every other
teammate** who wants to run the pipeline locally against real Firestore/GCS/Vertex
AI needs to run this once on their own laptop, in their own browser — it cannot be
scripted or delegated to an agent:
```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project sdoc-hackathon
```
After that, each teammate should verify locally: `.env` has `GCP_PROJECT`,
`GCS_BUCKET`, and `INBOX_SOURCE` set (`gs://sdoc-hackathon-attachments/dataset` for
the real data, or `http://localhost:8080` if running the Docker distribution
locally instead); run `uvicorn`, and confirm calls actually reach the real
Firestore project and GCS bucket, not a mock. A failure here is almost always one
specific misconfigured thing (wrong project, propagation delay, quota-project
mismatch) — get the exact error before changing code speculatively.

**Note on trusting this file:** this section previously said Firestore/Storage/IAM
were still to set up after they had already been completed and verified — the file
just hadn't been updated to match reality. An agent reading CLAUDE.md has no way to
know a "still to set up" line is stale; it will report exactly what's written here.
Whoever finishes a setup step from this file should edit it in the same sitting,
as its own commit, not fold the doc update into a feature commit.

## LLM auth — no API key needed by default (updated after Stage 2 build)
`llm_client.py` (built with Stage 2) defaults to **Vertex AI via ADC** — no Gemini
API key exists anywhere in this project, committed or otherwise. On Cloud Run, ADC
resolves to the service account, which now has `roles/aiplatform.user` (see Cloud
setup above); locally, it resolves to whatever `gcloud auth application-default
login` credentials are already on your machine. Nothing to provision, no secret to
rotate, and one less thing that can leak when the repo goes public.

`llm_client.py` falls back to the direct Gemini API-key backend **only** if
`GEMINI_API_KEY` is set in the environment — kept as an escape hatch (e.g. if Vertex
quota gets tight), not the primary path. If that fallback is ever actually used:

```bash
gcloud secrets create gemini-api-key --replication-policy="automatic"
printf "THE_KEY" | gcloud secrets versions add gemini-api-key --data-file=-
gcloud secrets add-iam-policy-binding gemini-api-key \
  --member="serviceAccount:328117535233-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```
Then in `deploy.sh`'s deploy block: `--set-secrets=GEMINI_API_KEY=gemini-api-key:latest`
(safe to commit — it's a reference, not a value).

Never commit keys regardless. Git history keeps them even if deleted later, and
flipping the repo public exposes anything in history. Confirm `.gitignore` covers
`.env`, `.env.local`, `credentials/*.json`. Commit a `.env.example` with dummy
values.

## Deployment decisions (from the Sep 20 workshop, considered and settled)
- **No CI/CD.** Cloud Build triggers were evaluated and deliberately skipped:
  `deploy.sh` already removes the friction, and wiring triggers means granting the
  Cloud Build SA `run.admin` + `iam.serviceAccountUser`, which is an IAM fight not
  worth the remaining hours. Revisit only if genuinely ahead.
- **No Vercel / frontend split.** See Frontend above.
- **No Postgres / Cloud SQL.** Firestore needs no instance, no pooling, no schema.
- **`--min-instances=1` before judging**, not now:
  `gcloud run services update sdoc-api --region=asia-southeast1 --min-instances=1`
  Stops scale-to-zero so judges never hit a cold start. Costs a couple of dollars
  across the judging window against the $300 credit.

## Repo structure
```
app/
  main.py         # FastAPI entrypoint, routes
  ingest.py       # Stage 1
  classify.py     # Stage 2
  validate.py     # Stage 3 — format detection, doc-type verification, reader routing
  readers/        # txt.py, pdf.py, docx.py, xlsx.py, ocr.py
  extract.py      # Stage 4
  compare.py      # Stage 5 — normalizers + comparator, NO LLM
  decide.py       # Stage 6 — status, evidence, audit record
  store.py        # Firestore + Cloud Storage access
  llm_client.py   # single interface wrapping Gemini
  schema.py       # canonical fields + submission output shape
templates/        # Jinja2 — review queue UI
scripts/
  deploy.sh
  score.py        # wraps inbox.submit(...)
data/             # participant bundle: inbox/, attachments/, loader.py
tests/golden_set/ # hand-labelled cases
```

## Task split
| Workstream | Covers |
| --- | --- |
| Ingest + Classify | Stages 1–2 |
| Validate + Extract | Stages 3–4 — **heaviest row**, two people if you have five |
| Normalize + Compare | Stage 5 — highest score-per-effort, give to the most meticulous person |
| Decide + Review UI | Stage 6 + Jinja templates + Firestore write-back |
| Cloud + Test harness | Deploy script, `/submit` scoring loop, golden set |
| Packaging | Deck, video, README — needs a name assigned now, not Monday night |

## Scope boundary (state this in the pitch)
The system verifies SI/BL **consistency**, not document **authenticity**. Sender
verification, malware scanning, signatures, issuer validation and alteration
detection belong to a separate authenticity module. Likely judge question.

## Known gotchas already hit
- `\` line continuation fails in `cmd.exe` — use one line, or `^`. Git Bash is fine.
- Docker Desktop must be running before `docker build`.
- Dockerfile must be named exactly `Dockerfile` for `docker build .` to find it.
- **Cloud Run injects `$PORT` at runtime — never hardcode a port.**
- Cloud Run local disk is ephemeral. Persist to Firestore/GCS, never to disk.
- Organizer Docker bundle (`sdoc-hackathon-docker`) contains the answer key at
  `data_v2/ground_truth.json`. **Do not open it.** Use `/submit` or `score_cli.py`
  only. Flagged to organizers.
- Cloud Run logs, not your terminal, hold Python tracebacks:
  `gcloud run services logs read sdoc-api --region=asia-southeast1 --limit=50`
- Revision history is a rollback button — Cloud Run → service → Revisions.

## Open items
- **`POST /api/run` is synchronous, ~3.5 min for the full 520-email dataset**, against
  Cloud Run's 300s default request timeout — fits today but with little margin.
  Decide before Stage 3+ needs to re-run at scale: either raise the deployed
  service's timeout (`gcloud run services update sdoc-api --region=asia-southeast1
  --timeout=<seconds>`), or add a background/async job pattern. Whoever owns
  Stage 3+ will also want a classify-only re-run mode that skips re-uploading
  attachments — not built yet, flagged by Claude Code as out of scope for the
  Stage 1–2 task.

## Progress — Stages 1–2 (done, committed 8c44d9b)
`schema.py`, `ingest.py`, `classify.py`, `llm_client.py` built and validated against
the real 520-email dataset: 520/520 cases in Firestore, 0 missing/extra ids, 0
classification errors, macro-F1 0.9987 scored via the organizers' `/submit` (no
ground truth opened). `ingest.py` strips quoted history and banners before
classification (195 quoted histories + 54 banners removed on the real inbox) and
writes attachments to GCS with sha256 in the inventory. Stages 3–6 and the review
templates are still untouched — next up per the task-split table above.