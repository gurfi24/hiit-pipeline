#!/usr/bin/env python3
"""
Lightweight Telegram poller -- no AI, no Garmin calls except spawning
analyze_workout.py on /start. Runs on a schedule (hourly at :30 via Task
Scheduler) and is the SOLE consumer of this bot's getUpdates stream, since
Telegram's update offset is a single shared cursor per bot.

Normal flow:
  - photo -> saved to boards/<send time>.jpg, queued in pending_store.
  - free text (or photo caption) -> queued in pending_store. A workout date in
    the text (19/9, "19 Sept", yesterday...) is picked up here; the send date
    is never used as the workout date. A short date-only message answers a
    "what date was the workout?" question from analyze_workout.py.
  - /start or "start" (case-insensitive) -> if paused, resumes first; then
    spawns analyze_workout.py in the background (the only place Garmin/LLM
    calls happen).
  - /stop or "STOP" (case-insensitive) -> writes automation_paused.flag.
    While paused, photos and texts are still SAVED (so nothing sent before
    /start is lost) but silently: no replies, no Garmin/LLM/vision calls, no
    alerts. Commands other than START/STOP are ignored. Only /start resumes.
  - /status -> pause state, pending dates, last analysis run.

Usage:
    python telegram_bot.py
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import env_setup

env_setup.load()  # populate os.environ from .env before anything reads GARMIN_*/TELEGRAM_*

import garmin_health
import pending_store

ROOT = Path(__file__).parent
ENV_PATH = ROOT / ".env"
STATE_PATH = ROOT / "data" / "telegram_state.json"
BOARDS_DIR = ROOT / "boards"
PAUSE_FLAG = ROOT / "automation_paused.flag"
LAST_RUN_PATH = ROOT / "data" / "last_run.json"
LOCAL_TZ = ZoneInfo("Asia/Jerusalem")

START_TEXTS = {"start", "/start"}
STOP_TEXTS = {"stop", "/stop"}


def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"last_update_id": 0, "chat_id": None}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def reply(base, chat_id, text):
    requests.post(f"{base}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=15)


def fmt_date(iso):
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m")


def store_text(base, chat_id, sent_at, text, notify=True):
    _, kind, wdate = pending_store.add_text(sent_at, text)
    if not notify:
        return
    if kind == "date_reply":
        reply(base, chat_id, f"📅 תאריך האימון נשמר: {fmt_date(wdate)}. שלח /start.")
    else:
        note = f" (תאריך האימון: {fmt_date(wdate)})" if wdate else ""
        reply(base, chat_id, f"📝 נשמר{note}. שלח /start כשהאימון סונכרן ב-Garmin.")


def handle_start(base, chat_id):
    was_paused = PAUSE_FLAG.exists()
    if was_paused:
        PAUSE_FLAG.unlink()
    reply(base, chat_id, "▶️ מנתח ניתוח... תקבל הודעה נפרדת כשזה יסתיים.")
    subprocess.Popen(
        [sys.executable, str(ROOT / "analyze_workout.py")],
        cwd=ROOT,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},  # Hebrew output must not crash on a cp1252 console
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
    )


def handle_stop(base, chat_id):
    PAUSE_FLAG.write_text(datetime.now().isoformat())
    reply(base, chat_id, "⏹️ נעצר. לא ייכנסו קריאות Garmin/LLM ולא יישלחו הודעות עד /start (תמונות וטקסטים ימשיכו להישמר בשקט).")


def handle_status(base, chat_id):
    lines = []

    if PAUSE_FLAG.exists():
        lines.append("⏸️ מצב: נעצר (שלח /start כדי להמשיך)")
    else:
        lines.append("▶️ מצב: פעיל, ממתין ל-/start")

    if LAST_RUN_PATH.exists():
        last = json.loads(LAST_RUN_PATH.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(last["timestamp"]).strftime("%d/%m %H:%M")
        lines.append(f"ניתוח אחרון: {ts} ({last.get('status')})")
        lines.append(f"  {last.get('message', '')}")
        if last.get("cost_usd") is not None:
            lines.append(f"  עלות: ${last['cost_usd']:.4f}")
    else:
        lines.append("ניתוח אחרון: עדיין לא רץ")

    pending = pending_store.entries()
    if pending:
        lines.append("ממתין לניתוח (/start):")
        for _, e in pending:
            sent = datetime.fromisoformat(e["sent_at"]).strftime("%d/%m %H:%M")
            wdate = f"אימון {fmt_date(e['workout_date'])}" if e["workout_date"] else "ללא תאריך אימון"
            lines.append(f"  נשלח {sent} -> {wdate}")
    else:
        lines.append("אין תמונות/טקסטים ממתינים.")

    garmin_state = garmin_health.get_status()
    if garmin_state.get("status") == "ok":
        last_success = garmin_state.get("last_success")
        ts = datetime.fromisoformat(last_success).strftime("%d/%m %H:%M") if last_success else "אף פעם"
        lines.append(f"Garmin: OK (שליפה אחרונה: {ts})")
    elif garmin_state.get("status") == "failing":
        since = garmin_state.get("failing_since")
        ts = datetime.fromisoformat(since).strftime("%d/%m %H:%M") if since else "?"
        lines.append(f"Garmin: נכשל מאז {ts} ({garmin_state.get('kind')})")
    else:
        lines.append("Garmin: עדיין לא נוסה")

    reply(base, chat_id, "\n".join(lines))


COMMANDS = {
    "/status": handle_status,
}


def main():
    env_setup.configure_stdio()
    token = load_env().get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN missing from .env")

    BOARDS_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    base = f"https://api.telegram.org/bot{token}"

    resp = requests.get(f"{base}/getUpdates", params={"offset": state["last_update_id"] + 1, "timeout": 5}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        sys.exit(f"Telegram API error: {data}")

    updates = data["result"]
    max_update_id = state["last_update_id"]
    photos_saved = 0

    for upd in updates:
        max_update_id = max(max_update_id, upd["update_id"])
        msg = upd.get("message") or upd.get("channel_post")
        if not msg:
            continue
        state["chat_id"] = msg["chat"]["id"]
        chat_id = msg["chat"]["id"]
        paused = PAUSE_FLAG.exists()

        text = (msg.get("text") or "").strip()
        normalized = text.lower()

        # START/STOP work even while paused. Anything else is still saved
        # while paused (silently), so messages sent before /start are not lost.
        if normalized in START_TEXTS:
            handle_start(base, chat_id)
            continue
        if normalized in STOP_TEXTS:
            handle_stop(base, chat_id)
            continue

        photos = msg.get("photo")
        if photos:
            largest = photos[-1]
            file_resp = requests.get(f"{base}/getFile", params={"file_id": largest["file_id"]}, timeout=20)
            file_resp.raise_for_status()
            file_info = file_resp.json()
            if file_info.get("ok"):
                file_path = file_info["result"]["file_path"]
                sent_at = datetime.fromtimestamp(msg["date"], tz=timezone.utc).astimezone(LOCAL_TZ)
                dest = BOARDS_DIR / f"{sent_at:%Y-%m-%d_%H%M%S}.jpg"  # renamed to the workout date at analysis
                img_resp = requests.get(f"https://api.telegram.org/file/bot{token}/{file_path}", timeout=30)
                img_resp.raise_for_status()
                dest.write_bytes(img_resp.content)
                pending_store.add_photo(sent_at, f"boards/{dest.name}")
                wdate = None
                if msg.get("caption"):
                    _, _, wdate = pending_store.add_text(sent_at, msg["caption"].strip())
                photos_saved += 1
                print(f"Saved board photo -> {dest}")
                note = f" (תאריך האימון: {fmt_date(wdate)})" if wdate else ""
                if not paused:
                    reply(base, chat_id, f"📸 נשמר{note}. שלח את הטקסט עם המשקלים/תוצאות, ואז /start.")
            continue

        if not text:
            continue

        command = text.split()[0].split("@")[0] if text else ""
        handler = COMMANDS.get(command)
        if handler:
            if not paused:  # a paused bot sends nothing
                print(f"Handling command: {command}")
                handler(base, chat_id)
            continue

        # Anything else is a free-text result message.
        sent_at = datetime.fromtimestamp(msg["date"], tz=timezone.utc).astimezone(LOCAL_TZ)
        store_text(base, chat_id, sent_at, text, notify=not paused)

    state["last_update_id"] = max_update_id
    save_state(state)
    if not updates:
        print("No new updates.")
    else:
        print(f"Processed {len(updates)} update(s), {photos_saved} photo(s) saved.")


if __name__ == "__main__":
    main()
