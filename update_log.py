#!/usr/bin/env python3
"""
Regenerates hiit-workout-log.md from data/workouts.json: one compact line
per workout, sorted oldest to newest. Deterministic full regen (not
incremental appends) so re-runs never drift or duplicate.

Usage:
    python update_log.py [--data-dir ./data] [--out ./hiit-workout-log.md]
"""

import argparse
import json
from pathlib import Path

TYPE_LABELS = {"HIIT": "HIIT", "RUNNING": "Running", "CYCLING": "Cycling", "OTHER": "Other"}


def format_line(w):
    date = w["start_time"][:10]
    wtype = TYPE_LABELS.get(w["type"], w["type"])
    name = w.get("name") or wtype
    duration = w.get("duration_min")
    duration_str = f"{duration:g} min" if duration is not None else "? min"
    hr = w.get("avg_hr")
    hr_str = f", avg HR {round(hr)}" if hr else ""
    source = w.get("source", "?")
    return f"- {date} | {wtype} | {name} | {duration_str}{hr_str} | source: {source}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="./data")
    parser.add_argument("--out", default="./hiit-workout-log.md")
    args = parser.parse_args()

    store = json.loads((Path(args.data_dir) / "workouts.json").read_text())
    workouts = sorted(store["workouts"], key=lambda w: w["start_time"])

    lines = [
        "# HIIT Workout Log",
        "",
        f"Compact one-line-per-workout log, regenerated from `data/workouts.json`. "
        f"{len(workouts)} workouts total.",
        "",
    ]
    lines.extend(format_line(w) for w in workouts)
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(workouts)} lines to {args.out}")


if __name__ == "__main__":
    main()
