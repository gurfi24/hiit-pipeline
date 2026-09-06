#!/usr/bin/env python3
"""
Lightweight Telegram poller -- no AI, no Claude Code invocation except when
explicitly triggered by /run_now. Runs frequently (hourly at :30 via Task
Scheduler) and is the SOLE consumer of this bot's getUpdates stream, since
Telegram's update offset is a single shared cursor per bot: a second
independent poller (e.g. the old telegram_sync.py) would race it and drop
messages. This script absorbed telegram_sync.py's photo-download job too.

Commands:
  /run_now  - spawns run_pipeline.py --force in the background, replies, exits
  /pause    - creates automation_paused.flag
  /resume   - removes automation_paused.flag
  /status   - reports last run info, cost, pending/flagged photos, pause state

Usage:
    python telegram_bot.py
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).parent
ENV_PATH = ROOT / ".env"
STATE_PATH = ROOT / "data" / "telegram_state.json"
BOARDS_DIR = ROOT / "boards"
PAUSE_FLAG = ROOT / "automation_paused.flag"
LAST_RUN_PATH = ROOT / "data" / "last_run.json"
LOCAL_TZ = ZoneInfo("Asia/Jerusalem")


def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"last_update_id": 0, "chat_id": None}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def reply(base, chat_id, text):
    requests.post(f"{base}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=15)


def handle_run_now(base, chat_id):
    reply(base, chat_id, "🚀 מפעיל את הפייפליין המלא עכשיו... תקבל הודעה נפרדת כשזה יסתיים.")
    subprocess.Popen(
        [sys.executable, str(ROOT / "run_pipeline.py"), "--force"],
        cwd=ROOT,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
    )


def handle_pause(base, chat_id):
    PAUSE_FLAG.write_text(datetime.now().isoformat())
    reply(base, chat_id, "⏸️ האוטומציה הושהתה. ריצות מתוזמנות ידלגו עד /resume.")


def handle_resume(base, chat_id):
    if PAUSE_FLAG.exists():
        PAUSE_FLAG.unlink()
    reply(base, chat_id, "▶️ האוטומציה פעילה שוב.")


def handle_status(base, chat_id):
    lines = []

    if PAUSE_FLAG.exists():
        lines.append("⏸️ מצב: מושהה")
    else:
        lines.append("▶️ מצב: פעיל")

    if LAST_RUN_PATH.exists():
        last = json.loads(LAST_RUN_PATH.read_text())
        ts = datetime.fromisoformat(last["timestamp"]).strftime("%d/%m %H:%M")
        lines.append(f"ריצה אחרונה: {ts} ({last.get('status')})")
        lines.append(f"  {last.get('message', '')}")
        if last.get("cost_usd") is not None:
            lines.append(f"  עלות: ${last['cost_usd']:.4f}")
    else:
        lines.append("ריצה אחרונה: עדיין לא רצה אף פעם")

    try:
        r = subprocess.run(
            [sys.executable, str(ROOT / "match_photos.py"),
             "--data-dir", str(ROOT / "data"), "--boards-dir", str(BOARDS_DIR), "--json"],
            capture_output=True, text=True, timeout=30,
        )
        match_result = json.loads(r.stdout.strip().splitlines()[-1])
        if match_result["pending"]:
            lines.append(f"תמונות ממתינות (אין אימון תואם): {', '.join(match_result['pending'])}")
        if match_result["flagged"]:
            lines.append(f"מסומן לבדיקה ידנית (תיקו): {', '.join(match_result['flagged'])}")
        if not match_result["pending"] and not match_result["flagged"]:
            lines.append("אין תמונות ממתינות או מסומנות.")
    except Exception as e:
        lines.append(f"(לא הצלחתי לבדוק תמונות ממתינות: {e})")

    reply(base, chat_id, "\n".join(lines))


COMMANDS = {
    "/run_now": handle_run_now,
    "/pause": handle_pause,
    "/resume": handle_resume,
    "/status": handle_status,
}


def main():
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

        photos = msg.get("photo")
        if photos:
            largest = photos[-1]
            file_resp = requests.get(f"{base}/getFile", params={"file_id": largest["file_id"]}, timeout=20)
            file_resp.raise_for_status()
            file_info = file_resp.json()
            if file_info.get("ok"):
                file_path = file_info["result"]["file_path"]
                msg_date_local = (
                    datetime.fromtimestamp(msg["date"], tz=timezone.utc).astimezone(LOCAL_TZ).date()
                )
                dest = BOARDS_DIR / f"{msg_date_local.isoformat()}.jpg"
                img_resp = requests.get(f"https://api.telegram.org/file/bot{token}/{file_path}", timeout=30)
                img_resp.raise_for_status()
                dest.write_bytes(img_resp.content)
                photos_saved += 1
                print(f"Saved board photo -> {dest}")
            continue

        text = (msg.get("text") or "").strip()
        command = text.split()[0].split("@")[0] if text else ""
        handler = COMMANDS.get(command)
        if handler:
            print(f"Handling command: {command}")
            handler(base, chat_id)

    state["last_update_id"] = max_update_id
    save_state(state)
    if not updates:
        print("No new updates.")
    else:
        print(f"Processed {len(updates)} update(s), {photos_saved} photo(s) saved.")


if __name__ == "__main__":
    main()
