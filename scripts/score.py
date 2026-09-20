#!/usr/bin/env python3
"""Score the current Firestore state against the organizer's /submit endpoint.

    python scripts/score.py                     # full raw scoreboard JSON
    python scripts/score.py --summary           # stage-1 numbers + confusion only
    python scripts/score.py --save out.json     # also write the submission we sent

Builds submission.json for every id in sample_submission.json (projection over
Firestore cases, so unprocessed emails still get a default entry), POSTs it to
SCORER_URL (default http://localhost:8080, the organizer Docker server), and
prints the response verbatim. Never touches the ground truth file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app import store  # noqa: E402
from app.ingest import get_inbox  # noqa: E402
from data.loader import Inbox  # noqa: E402

SCORER_URL = os.environ.get("SCORER_URL", "http://localhost:8080")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--summary", action="store_true", help="print stage-1 metrics and confusion only")
    ap.add_argument("--save", metavar="PATH", help="write the submitted submission.json here too")
    args = ap.parse_args()

    sample = get_inbox().sample_submission()
    submission = store.build_submission(sample.keys())
    missing = set(sample) - set(submission)
    extra = set(submission) - set(sample)
    print(f"submission entries: {len(submission)} | sample ids: {len(sample)} | exact match: {not missing and not extra}",
          file=sys.stderr)
    if missing or extra:
        print(f"  missing: {sorted(missing)}\n  extra: {sorted(extra)}", file=sys.stderr)

    if args.save:
        Path(args.save).write_text(json.dumps(submission, indent=2), encoding="utf-8")
        print(f"submission written to {args.save}", file=sys.stderr)

    result = Inbox(SCORER_URL).submit(submission)

    if args.summary:
        s1 = result["stage1"]
        print(f"stage1  accuracy={s1['accuracy']:.4f}  macro_f1={s1['macro_f1']:.4f}")
        for cat, m in s1["per"].items():
            print(f"  {cat:15s} tp={m['tp']:4d} fp={m['fp']:4d} fn={m['fn']:4d}")
        print("confusion (gold -> predicted):", json.dumps(s1["confusion"]))
        print(f"stage3  defect_f1={result['stage3']['defect_f1']:.4f}  field_f1={result['stage3']['field_f1']:.4f}")
        print(f"reliability  escalation_f1={result['reliability']['escalation_f1']:.4f}")
        print(f"end_to_end  {result['end_to_end']['success']}/{result['end_to_end']['total']}")
        print(f"final_score={result['final_score']:.4f}")
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
