# SDOC Hackathon — Project Context

**Event:** Averis x Monash Hackathon 2026. Team of 2–5. Problem: shipping document
verification (email inbox → classify → compare SI vs BL → discrepancy report).

## Timeline
- Prelim submission: **Tue 22 Sep, 12:00 PM** (GitHub repo, live deployment, slide
  deck, demo video — length is disputed between rules doc (5 min) and event site
  (3 min); confirm with organizers, penalty is 1 mark per 30s over).
- Feature freeze target: Sun 20 midnight. Mon 21 = packaging only (README, deck,
  video), not building.
- Finals (if shortlisted): 26 Sep. Final submission must be an *extension* of
  the prelim entry — don't build throwaway code.

## Scoring — what actually earns points
Prelim rubric (100 pts): Working Core Prototype 25, System Design & Architecture
15, Technology Integration 15, Technical Feasibility & Validation 15, Problem
Statement Understanding 10, Innovation & Solution Approach 10, Practical Value
10. **65 points are "it works, architecture is sound, you can prove it."**

Self-eval formula: 50% end-to-end (defects caught fully) + 30% Stage-1 macro-F1
+ 20% Stage-3 defect-F1. `NEEDS_REVIEW` handling scored as a **separate
reliability axis, outside the above** — escalating a case you could have scored
forfeits points on both end-to-end and defect-F1. Escalate only when you
genuinely cannot decide, not to hedge.

Two hard rules: AI must be a key component (no pure-regex solution). Cloud
infra must be *meaningfully* integrated (named managed services doing real
work — storage, queue, managed AI — not just a hosted frontend) or scores may
be reduced significantly.

## Output contract (must match exactly)
```
submission[email_id] = {
  "category":      "BL_COMPARISON" | "SI_REQUEST" | "INVOICE_QUERY" | "GENERAL" | "SPAM",
  "status":         "OK" | "MISMATCH" | "NEEDS_REVIEW",      # BL_COMPARISON only, else null
  "review_reason":  "wrong_doc_type" | "missing_attachment" | "unreadable" | "missing_value" | null,
  "has_defect":     bool,
  "defect_fields":  [...]
}
```
Every `email_id` in the dataset must be present. Match `sample_submission.json` exactly.

**7 compared fields:** shipper, consignee, notify_party, port_of_loading,
port_of_discharge, container_count, gross_weight_kg. SI is the source of truth.
Field labels differ across documents (e.g. "Port of Loading" vs "Load Port") —
map to canonical schema during extraction, not during comparison.

## Architecture — 6-stage pipeline, one FastAPI service
0. **Ingest** — pull email + attachments via `loader.py`, write to Cloud Storage, create case record.
1. **Classify** — LLM call, structured JSON out, category + confidence. Use subject + body + attachment info, never subject alone (misleading subjects are a known trap in the dataset).
2. **Extract** — per attachment: identify doc type first (catches `wrong_doc_type`), then extract to canonical schema. Every field returns `{value, evidence_snippet, confidence}`, `value: null` if absent.
3. **Normalize + compare** — deterministic, no LLM. Handle: case/whitespace, corporate suffixes (Pte Ltd / Sdn Bhd / Inc), port aliases + UN/LOCODE, container count formats, weight units. Emit per-field reason code: `matched` | `matched_after_normalisation` | `mismatched` | `unreadable` | `missing`.
4. **Decide status** — a confident mismatch beats partial uncertainty; don't escalate to NEEDS_REVIEW if you've already found a real defect elsewhere.
5. **Review queue** — human-in-the-loop UI: SI/BL values side by side, evidence snippet, reason code, confirm/correct that writes back and updates the report.

Keep all LLM calls behind one interface (`llm_client.py`) — swap point if
rate-limited mid-hackathon.

## Cloud setup — already done manually, do not redo
- GCP project ID: `sdoc-hackathon`
- Region: `asia-southeast1` (Singapore — nearest to KL)
- Artifact Registry repo: `sdoc-repo` (docker format), created and working
- Billing: Free Trial, $300 credit / 90 days — should not incur real cost at this scale
- Cloud Run service: currently `sdoc-hello` (hello-world proof), **rename to
  `sdoc-api`** on first real deploy
- Deploy loop, proven working end-to-end:
  ```
  docker build -t hello-cloudrun .
  docker tag hello-cloudrun asia-southeast1-docker.pkg.dev/sdoc-hackathon/sdoc-repo/hello-cloudrun:v1
  docker push asia-southeast1-docker.pkg.dev/sdoc-hackathon/sdoc-repo/hello-cloudrun:v1
  gcloud run deploy sdoc-api --image=asia-southeast1-docker.pkg.dev/sdoc-hackathon/sdoc-repo/hello-cloudrun:v1 --region=asia-southeast1 --platform=managed --allow-unauthenticated
  ```
- **Cloud Run reads `$PORT` env var at runtime — never hardcode a port** in the
  app or Dockerfile CMD.
- Public reachability verified from a phone on cellular data, not just localhost/wifi.
- Not yet set up: IAM roles for Vertex AI / Firestore / Storage access from the
  Cloud Run service account (needed the moment real pipeline code calls
  Gemini/Firestore/Storage — will fail with PermissionDenied until granted).
  Secrets (Gemini API key) must go via `--set-env-vars` or Secret Manager,
  never hardcoded — repo will be public.

## Repo structure (target)
```
app/
  main.py         # FastAPI entrypoint
  ingest.py       # Stage 0
  classify.py     # Stage 1
  extract.py      # Stage 2
  compare.py      # Stage 3 — normalizers + comparator, no LLM
  decide.py       # Stage 4
  review.py       # Stage 5
  llm_client.py   # single interface wrapping Gemini
  schema.py       # canonical fields + submission output shape
frontend/         # review queue UI
scripts/
  deploy.sh       # wraps the 4-command deploy loop above
  score.py        # wraps inbox.submit(...) for local scoring
data/             # participant bundle: inbox/, attachments/, loader.py
tests/golden_set/ # hand-labelled ~10 emails for fast local iteration
```

## Known gotchas already hit
- `\` line continuation fails in `cmd.exe` — use one line, or `^` for continuation.
- Docker Desktop must be running before `docker build` — check tray icon.
- Dockerfile must be named exactly `Dockerfile`, no extension, for `docker build .` to find it without `-f`.
- Organizer Docker bundle (`sdoc-hackathon-docker`) contains the answer key at
  `/secrets` — do not open `ground_truth.json`. Use only `/submit` endpoint or
  `score_cli.py`. Flag this to organizers.

## Open items to confirm with organizers
- Video length: 3 min (event site) vs 5 min (rules doc) — confirm which governs.
- Any sponsor cloud credits available.
