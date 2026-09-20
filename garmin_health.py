#!/usr/bin/env python3
"""
Garmin connection health: wraps get_client()+fetch_recent() with one retry,
classifies failures, tracks state (+ a rate-limit cooldown) in
data/garmin_state.json, and sends throttled Telegram alerts. Every entry
point that can attempt a Garmin login (garmin_sync.py's CLI, scripts/
garmin_login.py, and analyze_workout.py's /start flow) goes through this
module so a failure is always classified, recorded, and reported the same
way -- and so none of them will ever hammer Garmin again during an active
rate-limit cooldown. Never raises to the caller -- callers get back an error
tuple, so a Garmin failure can never trigger an LLM call.
"""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from garmin_sync import (
    GarminLoginIncomplete,
    GarminMFARequired,
    GarminNotConfigured,
    fetch_recent,
    get_client,
    resolve_hr_zones,
)

ROOT = Path(__file__).parent
STATE_PATH = ROOT / "data" / "garmin_state.json"
RETRY_DELAY_SECONDS = 5
NOTIFY_COOLDOWN_SECONDS = 6 * 3600
RATE_LIMIT_COOLDOWN_HOURS = 3

# Kinds that a retry can plausibly fix (stale token, blip). MFA and rate-limit
# are not retried -- MFA needs a human, and retrying right after a 429 just
# makes the rate-limiting worse.
RETRYABLE_KINDS = {"login", "token", "network", "empty_response", "unknown"}

HOWTO = {
    "mfa": "run scripts\\garmin_login.py in a terminal to re-authenticate",
    "login": "run scripts\\garmin_login.py to re-authenticate",
    "token": "run scripts\\garmin_login.py to refresh the session",
    "rate_limit": "no action needed, this should resolve on its own -- Garmin is rate-limiting requests",
    "network": "check your internet/Garmin server status, this should resolve automatically",
    "empty_response": "if this keeps happening, run scripts\\garmin_login.py to refresh the session",
    "unknown": "if this keeps happening, run scripts\\garmin_login.py to refresh the session",
}
LABELS = {
    "mfa": "MFA נדרש",
    "login": "כשל התחברות",
    "token": "טוקן פג/לא תקף",
    "rate_limit": "חסימת קצב (rate limit)",
    "network": "שגיאת רשת",
    "empty_response": "תשובה ריקה/לא צפויה מ-Garmin",
    "unknown": "שגיאה לא ידועה",
}


def classify_error(exc):
    """(kind, short_reason). kind in: mfa, login, token, rate_limit, network, empty_response, unknown."""
    # Unwrap one level if this is itself a wrapper with the real cause
    # attached (garminconnect's login() wraps almost everything) -- prefer
    # classifying the real error, but fall back to the wrapper's own message
    # if unwrapping doesn't change anything.
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        kind, reason = classify_error(cause)
        if kind != "unknown":
            return kind, reason

    if isinstance(exc, GarminMFARequired):
        return "mfa", "Garmin is asking for an MFA code"
    if isinstance(exc, (GarminNotConfigured, GarminLoginIncomplete)):
        return "login", str(exc)

    name = type(exc).__name__
    msg = str(exc)[:200]

    # "Display name is not set" is garminconnect's internal guard for "you
    # called an API method without a completed login" -- a symptom of a
    # failed/incomplete login, never a real profile problem.
    if "display name is not set" in msg.lower():
        return "login", "Garmin API call made without a completed login (client.display_name was never set)"

    if "TooManyRequests" in name or "rate limit" in msg.lower() or " 429" in msg or "429)" in msg:
        return "rate_limit", "Garmin is rate-limiting requests (HTTP 429)"
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429:
            return "rate_limit", "Garmin is rate-limiting requests (HTTP 429)"
        if status in (401, 403):
            return "token", f"Garmin session/token rejected (HTTP {status})"
        return "unknown", f"Garmin HTTP error {status}"
    if "Authentication" in name:
        return "login", f"Garmin rejected the login/session: {msg}"
    if "Connection" in name or isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return "network", f"Network error reaching Garmin: {msg}"
    if isinstance(exc, (KeyError, TypeError, ValueError)):
        return "empty_response", f"Unexpected/empty Garmin response: {msg}"
    return "unknown", f"{name}: {msg}"


def _redact(text, *secrets):
    for s in secrets:
        if s:
            text = text.replace(s, "[redacted]")
    return text


def _load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"status": "unknown", "kind": None, "reason": None, "failing_since": None,
            "last_success": None, "last_notified": None, "cooldown_until": None}


def _save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # utf-8 explicitly -- Windows' default write_text() encoding is the
    # locale codepage (cp1252 here), which crashes on non-ASCII text.
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _send_telegram(token, chat_id, text):
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except requests.RequestException:
        pass  # best-effort -- don't crash the health check over a failed alert


def cooldown_status():
    """(blocked: bool, message: str|None). Checked by garmin_sync.py's CLI,
    scripts/garmin_login.py, and the /start flow BEFORE attempting any
    login -- protects against extending a Garmin IP rate-limit by retrying
    manually while one is already active."""
    state = _load_state()
    until = state.get("cooldown_until")
    if not until:
        return False, None
    retry_at = datetime.fromisoformat(until)
    if datetime.now() < retry_at:
        return True, f"Garmin rate-limit cooldown active until {retry_at.strftime('%Y-%m-%d %H:%M')} local time -- refusing to attempt login until then."
    return False, None


def _record_success(telegram_token, chat_id):
    state = _load_state()
    was_failing = state.get("status") == "failing"
    _save_state({
        "status": "ok", "kind": None, "reason": None, "failing_since": None,
        "last_success": datetime.now().isoformat(), "last_notified": None, "cooldown_until": None,
    })
    if was_failing:
        _send_telegram(telegram_token, chat_id, "✅ החיבור ל-Garmin חזר לפעול.")


def _record_failure(kind, reason, telegram_token, chat_id, email, password, github_token, bot_token):
    reason = _redact(reason, password, github_token, bot_token)
    state = _load_state()
    now = datetime.now()
    was_failing = state.get("status") == "failing"
    failing_since = state.get("failing_since") if was_failing else now.isoformat()
    last_notified = state.get("last_notified")
    should_notify = True
    if last_notified:
        elapsed = (now - datetime.fromisoformat(last_notified)).total_seconds()
        should_notify = elapsed >= NOTIFY_COOLDOWN_SECONDS

    cooldown_until = state.get("cooldown_until")
    if kind == "rate_limit":
        # Set (or extend) a hard cooldown -- garmin_sync.py, garmin_login.py,
        # and fetch_recent_safe() all refuse to even attempt a login while
        # this is active, so a human retrying manually can't make it worse.
        cooldown_until = (now + timedelta(hours=RATE_LIMIT_COOLDOWN_HOURS)).isoformat()

    _save_state({
        "status": "failing", "kind": kind, "reason": reason, "failing_since": failing_since,
        "last_success": state.get("last_success"),
        "last_notified": now.isoformat() if should_notify else last_notified,
        "cooldown_until": cooldown_until,
    })
    if should_notify:
        label = LABELS.get(kind, kind)
        howto = HOWTO.get(kind, HOWTO["unknown"])
        cooldown_line = ""
        if kind == "rate_limit":
            retry_at = datetime.fromisoformat(cooldown_until).strftime("%H:%M")
            cooldown_line = f"\nלא ננסה שוב אוטומטית לפני {retry_at}.\n"
        _send_telegram(
            telegram_token, chat_id,
            f"❌ Garmin נכשל: {label}\n{reason}\n"
            f"{cooldown_line}\n"
            f"מה לעשות: {howto}\n\n"
            "התמונה/טקסט שלך נשמרו "
            "במצב ממתין -- בפעם הבאה "
            "ש-Garmin יעבוד שלח /start שוב.",
        )


def get_status():
    return _load_state()


def _attempt_login_with_retry(telegram_token, chat_id, env):
    """Shared by fetch_recent_safe() and login_only_safe(): one get_client()
    attempt, one retry for plausibly-transient kinds. Returns (client, None)
    on success, or (None, (kind, reason)) on failure -- already recorded +
    (throttled) notified. Does NOT check the cooldown itself -- callers must
    call cooldown_status() first, since what happens on cooldown-block
    differs slightly by caller (CLI print vs. Telegram-only)."""
    email = env.get("GARMIN_EMAIL")
    password = env.get("GARMIN_PASSWORD")
    github_token = env.get("GITHUB_TOKEN")
    bot_token = env.get("TELEGRAM_BOT_TOKEN")

    attempts = 2
    for attempt in range(1, attempts + 1):
        try:
            client = get_client()
            return client, None
        except Exception as e:
            kind, reason = classify_error(e)
            retry_left = attempt < attempts and kind in RETRYABLE_KINDS
            if retry_left:
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            _record_failure(kind, reason, telegram_token, chat_id, email, password, github_token, bot_token)
            return None, (kind, reason)
    return None, ("unknown", "exhausted retries")


def login_only_safe(telegram_token, chat_id, env):
    """Just verify Garmin login works (no activity fetch) -- used by
    garmin_sync.py's CLI and scripts/garmin_login.py. Refuses immediately
    (no attempt at all) during an active rate-limit cooldown. Returns
    (client, None) on success, or (None, (kind, reason)) on failure/cooldown."""
    blocked, msg = cooldown_status()
    if blocked:
        return None, ("rate_limit", msg)

    client, err = _attempt_login_with_retry(telegram_token, chat_id, env)
    if err:
        return None, err
    _record_success(telegram_token, chat_id)
    return client, None


def fetch_recent_safe(days, telegram_token, chat_id, env):
    """get_client() + fetch_recent() with one retry (skipped for MFA/rate-limit,
    which a retry can't fix). Refuses immediately during an active rate-limit
    cooldown. Returns (activities, None) on success, or (None, (kind, reason))
    on failure -- callers must not call the LLM on the latter. Updates
    data/garmin_state.json and sends a throttled Telegram alert on failure /
    a one-time "restored" message on recovery."""
    blocked, msg = cooldown_status()
    if blocked:
        return None, ("rate_limit", msg)

    email = env.get("GARMIN_EMAIL")
    password = env.get("GARMIN_PASSWORD")
    github_token = env.get("GITHUB_TOKEN")
    bot_token = env.get("TELEGRAM_BOT_TOKEN")

    attempts = 2
    for attempt in range(1, attempts + 1):
        try:
            client = get_client()
            hr_zones = resolve_hr_zones(client, env=os.environ)
            activities = fetch_recent(client, hr_zones, days=days)
            if not isinstance(activities, list):
                raise ValueError("fetch_recent returned a non-list response")
            _record_success(telegram_token, chat_id)
            return activities, None
        except Exception as e:
            kind, reason = classify_error(e)
            retry_left = attempt < attempts and kind in RETRYABLE_KINDS
            if retry_left:
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            _record_failure(kind, reason, telegram_token, chat_id, email, password, github_token, bot_token)
            return None, (kind, reason)
    return None, ("unknown", "exhausted retries")
