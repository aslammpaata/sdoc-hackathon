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
cases/{email_id}                                    # written by:
  email: { subject, body, sender, cleaning }        # stage 1 (cleaned)
  raw_email: { subject, body, sender, attachments } # stage 1 (verbatim, audit)
  attachments: [ {filename, path, declared_type, size_bytes, sha256, gcs_uri} ]
  category, classification: { confidence, reason, model, prompt_version, error }  # stage 2
  review_reason                                     # stage 3 (null if it went on to compare)
  si: { shipper: {value, original_text, source, method, confidence}, ... }        # stage 4
  bl: { ... }
  comparison: { shipper: "matched", port_of_discharge: "mismatched", ... }         # stage 5
  status, has_defect, defect_fields                 # stage 6 (null status = no comparison attempted)
  audit: { ingested_at, classified_at, extracted_at, decided_at, file_hashes, readers_used,
           rule_version, decision_version, stage3_exit, decision_note, undecided_fields, error }
  review: { needed, resolved_by, note, corrections: {previous, status, defect_fields}, resolved_at }
```
Firestore `set(merge=True)` merges at leaf level, so each stage upserts only its own
keys via `store.upsert_case(email_id, **fields)`. Lists (`attachments`,
`defect_fields`) are replaced whole, not merged.

Two stage-6 rules other stages must respect:
- **A human decision wins.** Once `review.resolved_by` is set (review UI), re-running
  stage 6 or the whole pipeline leaves the contract fields alone. Only
  `python main.py decide --force` / `POST /api/decide?force=true` discards it.
- **Stage 3's exit reason lives in `audit.stage3_exit`**, because stage 6 rewrites
  `review_reason` (the 91 "please send the draft BL" requests get null). Never read
  `review_reason` to learn why stage 3 exited.

## Frontend
**Server-rendered from the same FastAPI app** — Jinja2 templates, no build step, no
CORS, no second deploy target, one URL. A review queue is a list, a detail view and
a correction form; a form post does that without a framework.

```
GET  /                → dashboard: counts, categories, readers, escalations, hard-case tour
GET  /cases           → explorer over all 520 (filters: status, category, reason; ?q= jumps to an id)
GET  /cases/{id}      → canonical detail: six-stage trace + audit exhibit + correction form
GET  /review          → cases where status == NEEDS_REVIEW (live query, never cached)
GET  /review/{id}     → 307 → /cases/{id}
POST /review/{id}     → write correction to Firestore, 303 → /review  (unchanged)
GET  /static/app.css  → the one stylesheet (tokens shared with the architecture diagram + deck)
GET  /api/submission  → submission.json projection                    (live)
POST /api/run?limit=&workers=  → stages 1–6 over the inbox, sync     (live, ~7 min)
POST /api/decide?force=        → stage 6 only, no LLM, ~45 s          (live)
GET  /health                                                          (live)
GET  /debug/store     → Firestore+GCS round-trip; 404 unless DEBUG_ROUTES=1 (never on Cloud Run)
```

API stays under `/api/*` so it never collides with UI routes.

Front-end rules (Sep 22 pass): the UI is **read-only over the case records** — the
only write is `POST /review/{id}`; there is deliberately no "re-run" control. `/`
and `/cases` read a 60 s in-process cache of all cases (stale-while-revalidate,
warmed in a thread at startup, `main._cases_cached`) because 520 Firestore reads
per page load is visibly slow; `/review` and `/cases/{id}` are always live so a
saved decision shows immediately. Stage-3 "verified as SI/BL" on the detail page is
derived from which file stage 4 cited as `source` — nothing new is stored. The
hard-case tour (`main.TOUR`) is a list of ids + blurbs; ids absent from the loaded
data are skipped, so a different dataset doesn't 404. Status is readable from shape
and colour, not just the word; `matched_after_normalisation` is drawn differently
from `matched` on purpose.

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
  the running image always traces to exact code. Sets `GCP_PROJECT`, `GCS_BUCKET`,
  `INBOX_SOURCE` env vars and pins `--timeout=1800`.
- Cloud Run request timeout: **1800s** (raised from the 300s default on Sep 20,
  revision `sdoc-api-00002-kc6`). Full-inbox `POST /api/run` takes ~7 min with
  stages 1–5 (was ~3.5 min for stages 1–2 alone).
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
teammate** who wants to run the pipeline locally needs to do it once on their own
laptop — follow **`SETUP.md`** (fresh laptop → running local server). Steps 5–6
there are the browser-only logins that cannot be scripted or delegated to an
agent; step 10 has the `/debug/store` round-trip check. A failure there is almost
always one specific misconfigured thing (wrong project, missing ADC login,
quota-project mismatch) — get the exact error before changing code speculatively.

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
- **`--min-instances=1` — ON since Sep 22** (revision `sdoc-api-00006-545`; first
  dashboard hit 0.14 s instead of a 5–15 s cold start). Costs a couple of dollars a
  day against the $300 credit. It persists across `deploy.sh` runs. **Turn it off
  after judging:**
  `gcloud run services update sdoc-api --region=asia-southeast1 --min-instances=0`

## Repo structure
```
main.py           # FastAPI entrypoint + run_pipeline() (repo root — Dockerfile runs `uvicorn main:app`)
app/
  ingest.py       # Stage 1 — get_inbox() (INBOX_SOURCE: http/gs/local), cleaning, attachment inventory
  classify.py     # Stage 2 — Gemini, subject + body + inventory in one prompt
  documents.py    # Stage 3 — format sniffed from bytes, readers (txt/pdf/docx/xlsx, vision for scans),
                  #   doc-type from content, validate_case() -> si/bl or review_reason
  extract.py      # Stage 4 — 7 fields per document into the canonical schema
  compare.py      # Stage 5 — normalizers + comparator, NO LLM
  decide.py       # Stage 6 — deterministic decision + audit; apply_correction() for the UI
  store.py        # Firestore + Cloud Storage access
  llm_client.py   # single interface wrapping Gemini (Vertex AI via ADC, or API key)
  schema.py       # canonical fields + submission output shape + build_submission()
templates/        # Jinja2 — base.html, dashboard.html, cases.html, case_detail.html, review_list.html
static/app.css    # the one stylesheet: light/dark tokens, IBM Plex, status + reason semantics
scripts/
  deploy.sh
  score.py        # POSTs the Firestore projection to the organizer /submit
data/loader.py    # participant loader; inbox/, attachments/, sample_submission.json are gitignored
                  #   (they live in gs://sdoc-hackathon-attachments/dataset)
tests/golden_set/ # hand-labelled cases  (NOT BUILT YET)
```
Scanned PDFs/images are read by Gemini's vision (`llm_client.generate(media=...)`) —
there is no OCR engine to install. Stage 3–5 deps: `pypdf`, `python-docx`, `openpyxl`.

## Task split
| Workstream | Covers | Status (Sep 21) |
| --- | --- | --- |
| Ingest + Classify | Stages 1–2 | **done** — `8c44d9b`, macro-F1 1.0 |
| Validate + Extract | Stages 3–4 | **done** — Am7-ys, merged `6a412c9` |
| Normalize + Compare | Stage 5 | **done** — same branch, defect-F1 1.0 with a provisional rule |
| Decide + Review UI | Stage 6 + Jinja templates + Firestore write-back | **done** — decision v1.0, 17-case queue, correction form writes back |
| Cloud + Test harness | Deploy script, `/submit` scoring loop, golden set | deploy + `scripts/score.py` done; golden set not |
| Packaging | Deck, video, README — needs a name assigned now, not Monday night | open; README has SETUP.md link only |

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
- **Python on Windows: always pass `encoding="utf-8"`** to `read_text`/`write_text`.
  The default is cp1252 — an em dash written that way broke `import main` with
  `SyntaxError: Non-UTF-8 code`, and a cp1252 read of a UTF-8 file mojibakes it.
- Vertex AI 429 `RESOURCE_EXHAUSTED` under a thread pool: `llm_client` defaults to
  the `global` endpoint, 4 workers, 8 retries — one regional endpoint with 8 workers
  dropped 11 of 520 calls. `python main.py retry-errors` re-runs failed classifications.
- The dataset plants truncated PDFs (`email_511_BL.pdf`, `email_515_BL.pdf`,
  pypdf says "EOF marker not found"). That's a review reason, not a bug to fix.
- The collaborator branch is `origin/stage-3-5` (hyphen), not `stage-3.5`.
- Claude Code's Bash tool silently truncates very long commands (~8 KB heredocs
  fail with "unexpected EOF"). Write the script to a file with the Write tool and
  run that instead.

## Open items
- **Stage-1 variance.** Gemini at temperature 0 still flips 2–3 "please find
  Shipping instruction for X: POL … Shipper …" emails between SI_REQUEST and
  BL_COMPARISON from run to run (219–223 BL_COMPARISON vs 220 gold). Stage 6 handles
  the flips correctly (null status, no escalation), so the cost is ≈ −0.001 on
  final. A fix belongs in classify.py (few-shot example or a "SI body pasted →
  SI_REQUEST" tie-break) — classify owner's call, not urgent.
- Vision-read scans (`email_512–514`) are decided as OK/MISMATCH although gold marks
  them `unreadable` — **settled: keep**. Reliability recall 0.85 = 17/20 is this.
  Say in the pitch: "we read scans the reference solution gave up on".
- Classify-only / analyse-only re-run mode: `python main.py run` redoes every stage
  including re-uploading attachments (~7 min). `decide` alone is 45 s.
- `--min-instances=1` is ON (Sep 22) — remember to turn it off after judging.
- The "Save decision" form is live on the public URL. A teammate saved a wrong
  MISMATCH on `email_501` (the invoice-posing-as-BL case) on Sep 21; it was
  reverted with `decide_case(case, force=True)`. Human decisions are kept across
  re-runs by design, so a stray click costs reliability points until reverted —
  check `review.resolved_by` across cases before submitting.
- `origin/lihong/UI` is superseded by the Sep 22 front-end pass (different token
  system); leave unmerged, delete when its owner agrees.

## Progress — Stages 1–6 + review UI done (as of Sep 21)
- **Stages 1–2** (`8c44d9b`): `ingest.py` strips quoted history + banners before
  classification (195 + 54 removed on the real inbox), inventories attachments with
  sha256 and writes them to GCS; `classify.py` is one Gemini call over subject +
  body + inventory. Stage-1 macro-F1 **1.0** on the last full run (0.9987 the run
  before — expect ±1 email between runs).
- **Stages 3–5** (Am7-ys, `origin/stage-3-5`, merged `6a412c9`): `documents.py`
  sniffs format from bytes, reads txt/pdf/docx/xlsx and hands scans to Gemini
  vision, picks SI/BL by content (98/98 SI, 89/89 BL txt files correct; the 5
  "BL" files that are commercial invoices are gold's 5 `wrong_doc_type`);
  `extract.py` one schema-constrained call per document; `compare.py`
  deterministic, UN/LOCODE port aliases with CJK/Arabic entries.
- **Last full run** (`python main.py run`, ~7 min): 520 processed, 0 classification
  errors, 0 analysis errors; readers txt 192 / xlsx 22 / pdf_text 20 / docx 8 /
  ocr 6 / unreadable 2; 117 cases compared, 72 field mismatches found.
- **Stage 6 + review UI** (`decide.py`, `templates/`, `/review*`): deterministic,
  idempotent, no LLM. Rule: non-comparison → null; zero attachments and no
  "attached/enclosed/dropped" claim → null (`no_documents_to_compare`); stage-3
  exit → NEEDS_REVIEW with that reason; any `mismatched` field → MISMATCH with
  exactly those fields (unreadable/missing don't block — confident mismatch wins);
  else unreadable → NEEDS_REVIEW/unreadable, missing → missing_value; else OK.
  Decision 1 was settled by probing `/submit`, not guessing: the 94 zero-attachment
  cases are 91 "please *send* the draft BL" (requests → null) + 3 "attachments
  appear to have been dropped" (real `missing_attachment`); escalating the 91 gave
  reliability P 0.157, null or OK both give P 1.0.
- **Validated** (`scripts/score.py`, full stages 1–6 run): final **0.9989**,
  end-to-end **46/46**, stage-3 defect-F1 **1.0**, reliability P **1.0** / R 0.85 /
  F1 **0.919**, 17 escalations (5 wrong_doc_type, 5 missing_attachment, 5
  unreadable, 2 missing_value). Contract invariants hold over all 520; a POST
  correction through `/review/{id}` leaves the queue and updates
  `/api/submission` immediately.
- **Front-end pass (Sep 22, `2713b2e`)** — read-only over frozen Firestore, no
  pipeline file touched: dashboard at `/`, `/cases` explorer with filters + jump
  box, `/cases/{id}` six-stage trace (ingest → classify → validate → extract →
  compare → decide, each with inputs and a conclusion; skipped stages say why) with
  the audit record promoted to a full exhibit (sha256 per file, GCS URI, rule and
  decision versions, per-stage timestamps); hard-case tour on the dashboard
  (`501` invoice-as-BL, `348` 毛重 label + composite port + party aliases, `013`
  port swapped with code kept, `517` placeholders, `507` one of two docs,
  `512–514` vision-read scans); one stylesheet on the deck's token system, dark
  mode, phone width. `/api/submission` proven byte-identical before/after.
  Finding: the dataset's non-English content is bilingual *labels*
  (`Gross Weight毛重(KGS)`, 54 cases), not CJK values — say "Chinese label" in
  the pitch, not "Chinese port".
- **Deployed Sep 22:** image `sdoc-api:2713b2e` → revision `sdoc-api-00006-545`
  (00005 = code, 00006 = `min-instances=1`), https://sdoc-api-he56zusm2a-as.a.run.app
  — verified live: `/` dashboard 0.14 s, `/cases` 520 rows, `?q=` 307, `/cases/{id}`
  six stages, `/review` 17 rows live, `/review/{id}` 307, `/static/app.css`,
  `/api/submission` 520 entries OK 66 / MISMATCH 46 / NEEDS_REVIEW 17 / null 391;
  `/debug/store` 404; timeout 1800; no errors in logs.