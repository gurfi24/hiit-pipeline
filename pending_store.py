#!/usr/bin/env python3
"""
Tracks board photos + free-text messages received via Telegram, waiting for
/start to trigger analysis. Persisted to data/pending.json so nothing is lost
across bot restarts/reboots.

An entry is one workout submission, keyed by the ISO send time of its first
message. The WORKOUT date lives in `workout_date` and comes from the content
(text, photo, or the user's reply) -- never from the send time:
  {"photo": "boards/<file>.jpg" or null, "texts": [str, ...],
   "sent_at": iso, "last_at": iso,
   "workout_date": "YYYY-MM-DD" or null, "date_source": "text"|"photo"|"user_reply"|null,
   "photo_date_checked": bool,   # the vision date read already ran (don't pay twice)
   "asked_date": bool}           # the user was asked for the date; their reply lands here
An entry is removed once analyze_workout.py successfully merges it.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import workout_date

ROOT = Path(__file__).parent
PENDING_PATH = ROOT / "data" / "pending.json"
ARCHIVE_PATH = ROOT / "data" / "pending_archive.json"
BOARDS_DIR = ROOT / "boards"
LOCAL_TZ = ZoneInfo("Asia/Jerusalem")

GROUP_GAP = timedelta(hours=2)  # messages closer than this belong to one submission
DATE_REPLY_MAX_CHARS = 40  # a short message with a date, sent after we asked, is a date reply
UNDATED_TTL_DAYS = 30  # undated entries wait this long for a date before being archived


def _dt(iso):
    d = datetime.fromisoformat(iso)
    return d if d.tzinfo else d.replace(tzinfo=LOCAL_TZ)


def _new_entry(sent_at):
    iso = sent_at.isoformat(timespec="seconds")
    return {
        "photo": None, "texts": [], "sent_at": iso, "last_at": iso,
        "workout_date": None, "date_source": None, "photo_date_checked": False, "asked_date": False,
    }


def _migrate(raw):
    """Old shape was {send_date: {photo, texts, received_at}}. The send date says
    nothing about the workout date, so those entries come back undated."""
    out = {}
    for key, e in raw.items():
        if "sent_at" in e:
            out[key] = e
            continue
        sent = (e.get("received_at") or f"{key}T12:00:00")[:19]
        new = _new_entry(datetime.fromisoformat(sent).replace(tzinfo=LOCAL_TZ))
        new["photo"], new["texts"] = e.get("photo"), list(e.get("texts", []))
        out[sent] = new
    return out


def load():
    if PENDING_PATH.exists():
        return _migrate(json.loads(PENDING_PATH.read_text(encoding="utf-8")))
    return {}


def save(pending):
    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    # utf-8 explicitly -- Windows' default write_text() encoding is the
    # locale codepage (cp1252 here), which crashes on Hebrew free-text.
    PENDING_PATH.write_text(json.dumps(pending, indent=2, ensure_ascii=False), encoding="utf-8")


def sent_local_date(entry):
    return _dt(entry["sent_at"]).astimezone(LOCAL_TZ).date()


def entries():
    """[(id, entry)] oldest first."""
    return sorted(load().items(), key=lambda kv: _dt(kv[1]["sent_at"]))


def _open_entry(pending, sent_at):
    """Latest entry whose last message is within GROUP_GAP of `sent_at`, else None."""
    if not pending:
        return None
    eid = max(pending, key=lambda k: _dt(pending[k]["last_at"]))
    return eid if abs(sent_at - _dt(pending[eid]["last_at"])) <= GROUP_GAP else None


def _create(pending, sent_at):
    eid = sent_at.isoformat(timespec="seconds")
    while eid in pending:
        eid += "_"
    pending[eid] = _new_entry(sent_at)
    return eid


def add_photo(sent_at, image_rel_path):
    """Attach to the open submission if it has no photo yet, else start a new one."""
    pending = load()
    eid = _open_entry(pending, sent_at)
    if eid is None or pending[eid]["photo"]:
        eid = _create(pending, sent_at)
    pending[eid]["photo"] = image_rel_path
    pending[eid]["last_at"] = sent_at.isoformat(timespec="seconds")
    save(pending)
    return eid


def add_text(sent_at, text):
    """Returns (entry_id, kind, workout_date_str_or_None); kind is "stored" or "date_reply"."""
    pending = load()
    found = workout_date.extract_date(text, sent_at)

    # Short date-only reply to our "what date was the workout?" question.
    if found and len(text.strip()) <= DATE_REPLY_MAX_CHARS:
        waiting = [k for k, e in sorted(pending.items(), key=lambda kv: _dt(kv[1]["sent_at"]))
                   if e["asked_date"] and not e["workout_date"]]
        if waiting:
            e = pending[waiting[0]]
            e["workout_date"], e["date_source"] = found.isoformat(), "user_reply"
            save(pending)
            return waiting[0], "date_reply", e["workout_date"]

    eid = _open_entry(pending, sent_at)
    if eid and found and pending[eid]["date_source"] == "text" and pending[eid]["workout_date"] != found.isoformat():
        eid = None  # a different workout date means a different workout
    if eid is None:
        eid = _create(pending, sent_at)
    e = pending[eid]
    e["texts"].append(text)
    e["last_at"] = sent_at.isoformat(timespec="seconds")
    if found and not e["workout_date"]:
        e["workout_date"], e["date_source"] = found.isoformat(), "text"
    save(pending)
    return eid, "stored", e["workout_date"]


def update(eid, **fields):
    pending = load()
    if eid in pending:
        pending[eid].update(fields)
        save(pending)


def remove(eid):
    pending = load()
    pending.pop(eid, None)
    save(pending)


def finalize_photo(eid, workout_date_str):
    """Rename the photo to boards/<workout date>.jpg (numbered if taken) and
    return its new relative path. No-op if the file is missing or already named."""
    pending = load()
    rel = pending[eid].get("photo")
    src = ROOT / rel if rel else None
    if not src or not src.exists():
        return rel
    dest = BOARDS_DIR / f"{workout_date_str}.jpg"
    if dest.resolve() == src.resolve():
        return rel
    n = 2
    while dest.exists():
        dest = BOARDS_DIR / f"{workout_date_str}_{n}.jpg"
        n += 1
    src.replace(dest)
    pending[eid]["photo"] = f"boards/{dest.name}"
    save(pending)
    return pending[eid]["photo"]


def prune_stale(days=workout_date.MAX_AGE_DAYS, undated_days=UNDATED_TTL_DAYS):
    """Archive (never silently discard) entries that can no longer be handled:
    dated ones whose workout date is older than `days`, and undated ones
    nobody dated within `undated_days` of the message date. Pure file I/O --
    never touches Garmin or the LLM. Returns the pruned [(id, entry)]."""
    pending = load()
    today = datetime.now(LOCAL_TZ).date()
    stale = {}
    for eid, e in pending.items():
        if e["workout_date"]:
            old = datetime.strptime(e["workout_date"], "%Y-%m-%d").date() < today - timedelta(days=days)
        else:
            old = _dt(e["sent_at"]).date() < today - timedelta(days=undated_days)
        if old:
            stale[eid] = e
    if not stale:
        return []

    archive = json.loads(ARCHIVE_PATH.read_text(encoding="utf-8")) if ARCHIVE_PATH.exists() else {}
    archive.update(stale)
    ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARCHIVE_PATH.write_text(json.dumps(archive, indent=2, ensure_ascii=False), encoding="utf-8")

    for eid in stale:
        pending.pop(eid)
    save(pending)
    return sorted(stale.items(), key=lambda kv: _dt(kv[1]["sent_at"]))
