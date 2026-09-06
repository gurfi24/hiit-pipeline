#!/usr/bin/env python3
"""
Polls the Telegram bot for new WOD board photos and saves them to
boards/<message-date>.jpg (local date). Tracks the last processed
update_id in a small state file so re-runs don't re-download photos.

Setup:
    TELEGRAM_BOT_TOKEN must be set in .env.

Usage:
    python telegram_sync.py [--boards-dir ./boards] [--state-file ./data/telegram_state.json]
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

LOCAL_TZ = ZoneInfo("Asia/Jerusalem")
ENV_PATH = Path(__file__).parent / ".env"


def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def load_state(state_path: Path):
    if state_path.exists():
        return json.loads(state_path.read_text())
    return {"last_update_id": 0, "chat_id": None}


def save_state(state_path: Path, state: dict):
    state_path.write_text(json.dumps(state, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--boards-dir", default="./boards")
    parser.add_argument("--state-file", default="./data/telegram_state.json")
    args = parser.parse_args()

    token = load_env().get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN missing from .env")

    boards_dir = Path(args.boards_dir)
    boards_dir.mkdir(parents=True, exist_ok=True)
    state_path = Path(args.state_file)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = load_state(state_path)

    base = f"https://api.telegram.org/bot{token}"
    resp = requests.get(
        f"{base}/getUpdates",
        params={"offset": state["last_update_id"] + 1, "timeout": 5},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        sys.exit(f"Telegram API error: {data}")

    updates = data["result"]
    saved = []
    max_update_id = state["last_update_id"]

    for upd in updates:
        max_update_id = max(max_update_id, upd["update_id"])
        msg = upd.get("message") or upd.get("channel_post")
        if not msg:
            continue
        state["chat_id"] = msg["chat"]["id"]

        photos = msg.get("photo")
        if not photos:
            continue

        largest = photos[-1]  # Telegram returns sizes ascending
        file_resp = requests.get(f"{base}/getFile", params={"file_id": largest["file_id"]}, timeout=20)
        file_resp.raise_for_status()
        file_info = file_resp.json()
        if not file_info.get("ok"):
            print(f"  getFile failed for update {upd['update_id']}: {file_info}")
            continue
        file_path = file_info["result"]["file_path"]

        msg_date_local = (
            datetime.fromtimestamp(msg["date"], tz=timezone.utc).astimezone(LOCAL_TZ).date()
        )
        dest = boards_dir / f"{msg_date_local.isoformat()}.jpg"

        img_resp = requests.get(f"https://api.telegram.org/file/bot{token}/{file_path}", timeout=30)
        img_resp.raise_for_status()
        dest.write_bytes(img_resp.content)
        saved.append(dest.name)
        print(f"Saved board photo -> {dest} (message sent {msg_date_local})")

    state["last_update_id"] = max_update_id
    save_state(state_path, state)

    print(f"No new board photos." if not saved else f"Downloaded {len(saved)} new board photo(s).")


if __name__ == "__main__":
    main()
