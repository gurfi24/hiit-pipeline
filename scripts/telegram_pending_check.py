#!/usr/bin/env python3
"""
Read-only look at what is waiting in the Telegram bot's getUpdates queue.

Safe by construction:
  - one GET to getUpdates with timeout=0 and NO offset. Telegram only drops
    updates when a later call passes an offset above their id, so this
    consumes nothing and cannot move the poller's cursor.
  - never writes anything: data/telegram_state.json is only READ (to tell
    already-processed updates that Telegram still redelivers from genuinely
    waiting ones).
  - prints only counts, types (photo / text / command / other), timestamps
    and whether a message looks like /start or STOP. Never message text,
    captions, file ids, chat ids or the token.

Do not run it while the poller runs (Telegram allows one getUpdates consumer).

Usage:
    python scripts/telegram_pending_check.py
"""

import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

import env_setup  # noqa: E402

env_setup.load()  # populate os.environ from .env before reading TELEGRAM_BOT_TOKEN

from telegram_bot import LOCAL_TZ, START_TEXTS, STOP_TEXTS  # noqa: E402

STATE_PATH = ROOT / "data" / "telegram_state.json"
GETUPDATES_LIMIT = 100  # Telegram's default page size; a full page may be truncated


def read_last_update_id():
    """The poller's cursor, or None if unreadable. Read-only."""
    try:
        return int(json.loads(STATE_PATH.read_text(encoding="utf-8"))["last_update_id"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def classify(msg):
    """(type, flag) for one message. flag is 'START', 'STOP' or ''. Never returns content."""
    if msg.get("photo"):
        return "photo", ""
    text = (msg.get("text") or "").strip()
    if not text:
        return "other", ""
    normalized = text.lower()
    flag = "START" if normalized in START_TEXTS else "STOP" if normalized in STOP_TEXTS else ""
    return ("command" if text.startswith("/") else "text"), flag


def describe(upd):
    msg = upd.get("message") or upd.get("channel_post")
    if not msg:
        return {"id": upd["update_id"], "when": None, "type": "other", "flag": ""}
    kind, flag = classify(msg)
    when = datetime.fromtimestamp(msg["date"], tz=timezone.utc).astimezone(LOCAL_TZ) if msg.get("date") else None
    return {"id": upd["update_id"], "when": when, "type": kind, "flag": flag}


def summarize(rows):
    counts = Counter(r["type"] for r in rows)
    parts = [f"{k}={counts[k]}" for k in ("photo", "text", "command", "other") if counts[k]]
    flags = Counter(r["flag"] for r in rows if r["flag"])
    parts += [f"{k}-like={flags[k]}" for k in ("START", "STOP") if flags[k]]
    return ", ".join(parts) or "nothing"


def main():
    env_setup.configure_stdio()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN missing from .env")

    try:
        # params: timeout only. Deliberately NO offset -- an offset would confirm (consume) updates.
        resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", params={"timeout": 0}, timeout=20)
    except requests.RequestException as exc:
        sys.exit(f"Telegram request failed ({type(exc).__name__})")  # never str(exc): it embeds the token URL
    if resp.status_code != 200:
        hint = " (another consumer or a webhook is active)" if resp.status_code == 409 else ""
        sys.exit(f"Telegram returned HTTP {resp.status_code}{hint}")
    data = resp.json()
    if not data.get("ok"):
        sys.exit("Telegram API error (ok=false)")

    rows = sorted((describe(u) for u in data["result"]), key=lambda r: r["id"])
    cursor = read_last_update_id()

    print("Telegram pending check -- read-only: no offset sent, nothing consumed, nothing written.")
    print(f"Updates returned by Telegram: {len(rows)}")
    if len(rows) >= GETUPDATES_LIMIT:
        print(f"  (full page of {GETUPDATES_LIMIT}: there may be more beyond this)")
    if cursor is None:
        print("Poller cursor unreadable: cannot tell processed from waiting; listing everything as 'unknown'.")
        waiting, processed = rows, []
    else:
        processed = [r for r in rows if r["id"] <= cursor]
        waiting = [r for r in rows if r["id"] > cursor]
        print(f"  already processed by the poller (Telegram redelivers until the next poll): {len(processed)}")
        print(f"  WAITING for the next poll: {len(waiting)}")

    for label, group in (("processed", processed), ("waiting", waiting)):
        for r in group:
            when = r["when"].strftime("%Y-%m-%d %H:%M") if r["when"] else "no timestamp"
            flag = f"  [looks like {r['flag']}]" if r["flag"] else ""
            print(f"  {label:9} #{r['id']}  {when}  {r['type']}{flag}")

    print(f"Waiting summary: {summarize(waiting)}")


if __name__ == "__main__":
    main()
