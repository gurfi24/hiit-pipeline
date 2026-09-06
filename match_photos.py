#!/usr/bin/env python3
"""
Matches WOD board photos (boards/<date>.jpg) to workout entries in
workouts.json by calendar date (local time, already the convention both
telegram_sync.py and garmin_sync.py write in).

Rules (see polar_telegram_plan.md section 3):
  - Only HIIT-type workouts are candidates for a photo match (no board photo
    is expected for runs/other activity types).
  - One photo, one HIIT workout on that date -> direct match.
  - Multiple HIIT workouts same date -> the longest-duration one wins; an
    exact duration tie is left unmatched and flagged for manual review.
  - No HIIT workout on a photo's date -> the photo stays pending, untouched.
  - "Most recently sent photo wins" is already handled upstream: telegram_sync.py
    always overwrites boards/<date>.jpg with the latest photo for that date,
    so there is at most one file per date by the time this script runs.

Usage:
    python match_photos.py [--data-dir ./data] [--boards-dir ./boards]
"""

import argparse
import json
from pathlib import Path


def load_workouts(data_dir: Path):
    path = data_dir / "workouts.json"
    return json.loads(path.read_text()), path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="./data")
    parser.add_argument("--boards-dir", default="./boards")
    parser.add_argument("--json", action="store_true", help="Print result as JSON instead of text (for /status)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    boards_dir = Path(args.boards_dir)
    store, path = load_workouts(data_dir)
    workouts = store["workouts"]

    by_date = {}
    for w in workouts:
        if w.get("type") != "HIIT":
            continue
        date_str = w["start_time"][:10]
        by_date.setdefault(date_str, []).append(w)

    matched, pending, flagged = 0, [], []

    for photo in sorted(boards_dir.glob("*.jpg")):
        date_str = photo.stem  # boards/<date>.jpg, date is YYYY-MM-DD
        candidates = by_date.get(date_str, [])
        image_rel = f"boards/{photo.name}"

        if not candidates:
            pending.append(date_str)
            continue

        if len(candidates) == 1:
            candidates[0]["image"] = image_rel
            matched += 1
            continue

        # Multiple HIIT workouts same date: longest duration wins.
        candidates.sort(key=lambda w: w.get("duration_min", 0), reverse=True)
        top = candidates[0]["duration_min"]
        tied = [c for c in candidates if c.get("duration_min") == top]
        if len(tied) > 1:
            flagged.append(date_str)
            continue

        candidates[0]["image"] = image_rel
        matched += 1

    path.write_text(json.dumps(store, indent=2, default=str))

    if args.json:
        print(json.dumps({"matched": matched, "pending": pending, "flagged": flagged}))
        return

    print(f"Matched {matched} photo(s) to workouts.")
    if pending:
        print(f"Pending (no HIIT workout on that date yet): {', '.join(pending)}")
    if flagged:
        print(f"Flagged for manual review (ambiguous same-duration tie): {', '.join(flagged)}")
    if not (matched or pending or flagged):
        print("No board photos found.")


if __name__ == "__main__":
    main()
