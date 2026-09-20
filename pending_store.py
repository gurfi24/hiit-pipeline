#!/usr/bin/env python3
"""
Tracks board photos + free-text messages received per local calendar date,
waiting for /start to trigger analysis. Persisted to data/pending.json so
nothing is lost across bot restarts/reboots.

Shape: {"<date>": {"photo": "boards/<date>.jpg" or null,
                    "texts": [str, ...],
                    "received_at": iso timestamp}}
An entry is removed once analyze_workout.py successfully merges it.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
PENDING_PATH = ROOT / "data" / "pending.json"
ARCHIVE_PATH = ROOT / "data" / "pending_archive.json"
LOCAL_TZ = ZoneInfo("Asia/Jerusalem")


def load():
    if PENDING_PATH.exists():
        return json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    return {}


def save(pending):
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    # utf-8 explicitly -- Windows' default write_text() encoding is the
    # locale codepage (cp1252 here), which crashes on Hebrew free-text.
    PENDING_PATH.write_text(json.dumps(pending, indent=2, ensure_ascii=False), encoding="utf-8")


def _entry(pending, date_str):
    return pending.setdefault(date_str, {"photo": None, "texts": [], "received_at": datetime.now().isoformat()})


def add_photo(date_str, image_rel_path):
    pending = load()
    _entry(pending, date_str)["photo"] = image_rel_path
    save(pending)


def add_text(date_str, text):
    pending = load()
    _entry(pending, date_str)["texts"].append(text)
    save(pending)


def remove(date_str):
    pending = load()
    pending.pop(date_str, None)
    save(pending)


def unanalyzed_dates():
    """Dates with at least a photo or some text, oldest first."""
    pending = load()
    return sorted(pending.keys())


def prune_stale(days=7):
    """Drop pending entries older than `days` from today (local date) -- they
    can never be matched by fetch_recent's lookback window anyway, so leaving
    them queued would just make every /start report "still waiting" forever.
    Archived (not silently discarded) to data/pending_archive.json. Pure file
    I/O -- never touches Garmin or the LLM. Returns the pruned date strings."""
    pending = load()
    cutoff = datetime.now(LOCAL_TZ).date() - timedelta(days=days)
    stale = {d: e for d, e in pending.items() if datetime.strptime(d, "%Y-%m-%d").date() < cutoff}
    if not stale:
        return []

    archive = json.loads(ARCHIVE_PATH.read_text(encoding="utf-8")) if ARCHIVE_PATH.exists() else {}
    archive.update(stale)
    ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVE_PATH.write_text(json.dumps(archive, indent=2, ensure_ascii=False), encoding="utf-8")

    for d in stale:
        pending.pop(d, None)
    save(pending)
    return sorted(stale.keys())
