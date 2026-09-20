# Local setup — fresh laptop to a running server

Follow the steps in order. Every command is meant to be pasted as-is into a
terminal. Commands are shown for **Git Bash on Windows** (what the team uses);
on macOS/Linux use the normal Terminal and skip the Windows-only notes.

> **Two steps open a browser and need you personally** — steps 5 and 6.
> They sign *you* in to Google. Nobody can run them for you: not a teammate,
> not a script, not an AI agent. Everything else can be copy-pasted.

You need a Google account that has been added to the `sdoc-hackathon` GCP
project. If you haven't been added yet, ask in the group chat before step 5.

---

## 1. Install Git

- Windows: download and run the installer from <https://git-scm.com/download/win>.
  Accept the defaults. This also installs **Git Bash** — use it for every
  command below (Start menu → "Git Bash").
- macOS: `xcode-select --install` in Terminal, or <https://git-scm.com/download/mac>.

Check:
```bash
git --version
```

## 2. Install Python 3.12 or newer

- Windows: <https://www.python.org/downloads/> → run the installer → **tick
  "Add python.exe to PATH"** on the first screen (easy to miss).
- macOS: `brew install python` or the installer from the same page.

Check (either `python` or `python3` should print 3.12+):
```bash
python --version
```

## 3. Install Docker Desktop (only needed to deploy or run the organizer inbox)

- <https://www.docker.com/products/docker-desktop/> → install → open it once
  and let it finish starting. On Windows it may ask you to enable WSL 2; say yes
  and reboot if prompted.
- **Docker Desktop must be running** (whale icon in the tray) whenever you run
  `docker` or `./scripts/deploy.sh`.

Check:
```bash
docker --version
```

## 4. Install the Google Cloud SDK (`gcloud`)

- Windows: <https://cloud.google.com/sdk/docs/install> → "Google Cloud CLI
  installer" → run it. Untick "Run gcloud init" at the end (we do it manually
  below). **Close and reopen Git Bash** afterwards so `gcloud` is on PATH.
- macOS: `brew install --cask google-cloud-sdk`, or the same page.

Check:
```bash
gcloud --version
```

## 5. Sign in to gcloud  🔒 *browser — do this yourself*

This lets the `gcloud` **command-line tool** act as you (deploys, listing
buckets, reading logs).

```bash
gcloud auth login
```
A browser tab opens → pick the Google account that was added to the project →
Allow. Then point gcloud at our project:

```bash
gcloud config set project sdoc-hackathon
gcloud config set run/region asia-southeast1
```

Check — should print your email and `sdoc-hackathon`:
```bash
gcloud config list
```

## 6. Sign in for the Python libraries (ADC)  🔒 *browser — do this yourself*

Step 5 only covered the `gcloud` tool. The **Python code** (Firestore, Cloud
Storage, Vertex AI clients) uses a separate credential called *Application
Default Credentials*. Without this step the server starts but every Firestore
or GCS call fails with `DefaultCredentialsError` or `PermissionDenied`.

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project sdoc-hackathon
```
Same browser flow as step 5 (same account). The second command is required —
without it you'll see warnings about a missing quota project and some API
calls are rejected.

## 7. Clone the repo and install dependencies

```bash
cd ~/Projects   # or wherever you keep code; create it first if needed: mkdir -p ~/Projects
git clone https://github.com/aslammpaata/sdoc-hackathon.git
cd sdoc-hackathon
pip install -r requirements.txt
```

If `pip` isn't found, try `python -m pip install -r requirements.txt`.
(A virtualenv is fine if you prefer one — `python -m venv .venv && source .venv/Scripts/activate` on Windows Git Bash, `source .venv/bin/activate` on macOS — but it's optional.)

## 8. Create your `.env`

`.env` holds local configuration and is gitignored — never commit it.

```bash
cp .env.example .env
```

Open `.env` in any editor and set these three to the real values:

```
GCP_PROJECT=sdoc-hackathon
GCS_BUCKET=sdoc-hackathon-attachments
INBOX_SOURCE=gs://sdoc-hackathon-attachments/dataset
```

Leave `GEMINI_API_KEY` **empty** (or delete the line): with no key the code uses
Vertex AI through the ADC login from step 6, which is the configured path. Leave
`DEBUG_ROUTES=0` unless you are running the round-trip check in step 10.

`INBOX_SOURCE` alternatives:
- `gs://sdoc-hackathon-attachments/dataset` — the uploaded dataset in Cloud
  Storage. This is what the deployed service uses; use it by default.
- `http://localhost:8080` — the organizer's Docker inbox server, if you are
  running it locally (it also exposes `/submit` for scoring).

## 9. Let Docker push to our registry (only if you will deploy)  🔒 *browser-free, but needs step 5*

```bash
gcloud auth configure-docker asia-southeast1-docker.pkg.dev
```
Answer `Y`. Skip this entirely if you won't run `./scripts/deploy.sh`.

## 10. Run the server locally

```bash
uvicorn main:app --reload
```

You should see `Uvicorn running on http://127.0.0.1:8000`. In a browser or a
second Git Bash window:

```bash
curl http://127.0.0.1:8000/health
```
→ `{"healthy":true}`. Interactive API docs are at <http://127.0.0.1:8000/docs>.

**Optional: prove the GCP wiring works end to end.** Stop the server (Ctrl+C)
and restart it with the debug route enabled, then call it:

```bash
DEBUG_ROUTES=1 uvicorn main:app --reload
# in another window:
curl http://127.0.0.1:8000/debug/store
```
A healthy result has `"attachment_roundtrip": true`, `"cleaned_up": true` and
`"email_count": 520`. It writes and immediately deletes one probe record in the
real Firestore project and bucket. If it errors, the message names the exact
problem (wrong project, missing ADC login, quota project) — fix that one thing
rather than changing code.

---

## Everyday commands

| What | Command |
| --- | --- |
| Start the server | `uvicorn main:app --reload` |
| Run stages 1–2 over the whole inbox | `python main.py run` (about 3–4 min) |
| Run on the first N emails only | `python main.py run 20` |
| Re-run only cases whose classification errored | `python main.py retry-errors` |
| Current submission.json | `curl http://127.0.0.1:8000/api/submission` |
| Score against the organizer server | `python scripts/score.py --summary` (needs the Docker inbox running on :8080; raw JSON without `--summary`) |
| Deploy to Cloud Run | `git pull` first, then `./scripts/deploy.sh` (Docker Desktop must be running; tell the group chat) |
| Read production logs | `gcloud run services logs read sdoc-api --region=asia-southeast1 --limit=50` |

## If something fails

| Symptom | Fix |
| --- | --- |
| `gcloud: command not found` after installing | Close and reopen Git Bash. |
| `DefaultCredentialsError` / `could not automatically determine credentials` | Step 6 wasn't done (or was done with a different account). |
| `403 PermissionDenied` on Firestore/GCS/Vertex | Your account isn't on the project yet — ask to be added as Editor. Then redo step 6. |
| Warning about "quota project" | Run the second command in step 6. |
| `python-dotenv` / `google.cloud` import errors | `pip install -r requirements.txt` again, with the same `python` you run uvicorn with. |
| `docker build` fails immediately | Docker Desktop isn't running. |
| `429 RESOURCE_EXHAUSTED` from Vertex during a run | Transient quota; the client retries automatically. Run `python main.py retry-errors` afterwards if the summary lists errors. |
