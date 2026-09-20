#!/usr/bin/env python3
"""
Loads this project's .env into the process environment (os.environ) at
startup. Path is resolved relative to THIS file (which lives at the project
root next to .env), not the current working directory -- so it works
whether a script is run by hand from anywhere, via Task Scheduler, or
spawned as a subprocess from another script in this project.

Every entry point (garmin_sync.py, analyze_workout.py, telegram_bot.py,
scripts/garmin_login.py, scripts/telegram_pending_check.py) must call
configure_stdio() first thing in main(), and load() before touching any GARMIN_*/
TELEGRAM_*/GITHUB_*/BIRTH_DATE env var.

Uses python-dotenv if installed; falls back to a small manual parser
otherwise. Never logs or prints values.
"""

from datetime import date
from pathlib import Path

ENV_PATH = Path(__file__).parent / ".env"


def compute_age(env=None):
    """Age in whole years from BIRTH_DATE (env dict, default os.environ). Never
    logs/prints the birth date -- only the resulting number leaves this
    function. None if unset/invalid."""
    import os

    birth_date_str = (env if env is not None else os.environ).get("BIRTH_DATE")
    if not birth_date_str:
        return None
    try:
        y, m, d = (int(p) for p in birth_date_str.strip().split("-"))
        birth = date(y, m, d)
    except (ValueError, TypeError):
        return None
    today = date.today()
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def configure_stdio():
    """Make this process (and every subprocess it spawns) UTF-8 safe on Windows.

    A Windows console/pipe defaults to the locale codepage (cp1252 here), so a
    plain print() of Hebrew or emoji text raises UnicodeEncodeError. Call this
    first thing in every entry script's main(): stdout/stderr are switched to
    UTF-8 with errors="replace" (a print can never crash the run) and
    PYTHONIOENCODING=utf-8 is exported so children inherit the same behavior.
    """
    import os
    import sys

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)  # absent on StringIO etc.
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    os.environ["PYTHONIOENCODING"] = "utf-8"


def load():
    try:
        from dotenv import load_dotenv
    except ImportError:
        _load_fallback()
        return
    # override=False: an already-set real env var wins over .env.
    load_dotenv(dotenv_path=ENV_PATH, override=False)


def _load_fallback():
    import os

    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()
