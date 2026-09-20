#!/usr/bin/env python3
"""
Manual interactive Garmin re-authentication. Run this by hand from a terminal
whenever /status or a Telegram alert says Garmin needs re-auth (expired
session, rejected token, MFA required). get_client() already prompts for an
MFA code on its own when a real terminal is attached, so this script is just
a plain entry point for that -- it does not duplicate the login logic.

Goes through garmin_health.py like every other Garmin entry point in this
project, so it refuses to attempt a login during an active rate-limit
cooldown (protects you from extending a block by retrying manually), and any
failure is classified + reported the same way /status and Telegram alerts do.

Usage:
    python scripts\\garmin_login.py
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import env_setup  # noqa: E402

env_setup.load()  # populate os.environ from .env before anything reads GARMIN_EMAIL/PASSWORD

import garmin_health  # noqa: E402


def main():
    env = {k: os.environ.get(k) for k in ("GARMIN_EMAIL", "GARMIN_PASSWORD", "GITHUB_TOKEN", "TELEGRAM_BOT_TOKEN")}
    telegram_token = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = None
    state_path = ROOT / "data" / "telegram_state.json"
    if state_path.exists():
        chat_id = json.loads(state_path.read_text(encoding="utf-8")).get("chat_id")

    client, err = garmin_health.login_only_safe(telegram_token, chat_id, env)
    if err:
        kind, reason = err
        sys.exit(f"Garmin login failed ({kind}): {reason}")

    print(f"Logged in as {getattr(client, 'display_name', '(unknown)')}. Session cached to ~/.garminconnect.")


if __name__ == "__main__":
    main()
