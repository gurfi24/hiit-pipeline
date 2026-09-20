#!/usr/bin/env python3
"""
The /start-triggered analysis run. Processes the MOST RECENT pending date
(photo and/or free text received via Telegram, not yet analyzed) -- older
pending dates are left queued and just mentioned in the summary message:
  1. Drop pending dates older than the Garmin lookback window (they can never
     be matched, so leaving them queued would spam "still waiting" forever).
  2. Pull the last 7 days of Garmin activities via garmin_health (one retry,
     failure classification, Telegram alert on failure -- deterministic, no
     LLM) and match this date's workout (local calendar date; longest
     duration wins on a multi-match day).
  3. If no Garmin match yet, tell the user and leave the pending entry alone.
  4. If matched, make ONE Claude Code headless call (the only step that costs
     money) to read the board photo + any free text, extract details, compare
     against similar past workouts in workouts.json, write 3-10 short
     insights, and add ONE new entry to workouts.json.
  5. Regenerate hiit-workout-log.md, commit + push, send one Telegram summary,
     and clear the pending entry.

Hard budget cap: --max-budget-usd enforces $0.50/workout in the CLI itself
(not just post-hoc monitoring). If it trips, we fail loudly via Telegram and
keep the pending entry untouched so nothing is lost.

Invoked by telegram_bot.py when it sees /start or "start" (case-insensitive).
Usage: python analyze_workout.py
"""

import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import requests

import env_setup

env_setup.load()  # populate os.environ from .env before anything reads GARMIN_*/TELEGRAM_*/BIRTH_DATE

import garmin_health
import pending_store

ROOT = Path(__file__).parent
ENV_PATH = ROOT / ".env"
PAUSE_FLAG = ROOT / "automation_paused.flag"
LAST_RUN_PATH = ROOT / "data" / "last_run.json"
WORKOUTS_PATH = ROOT / "data" / "workouts.json"
BUDGET_CEILING_USD = 0.50
GARMIN_LOOKBACK_DAYS = 7


def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


compute_age = env_setup.compute_age  # shared with garmin_sync's max-HR age fallback


def send_telegram(token, chat_id, text):
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"  (failed to send Telegram message: {e})")


def write_last_run(status, message, cost_usd=None, new_workouts=0):
    LAST_RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_RUN_PATH.write_text(json.dumps({
        "timestamp": datetime.now().isoformat(),
        "status": status,
        "message": message,
        "cost_usd": cost_usd,
        "new_workouts": new_workouts,
    }, indent=2), encoding="utf-8")


def find_git_exe():
    import shutil
    found = shutil.which("git")
    if found:
        return found
    for candidate in (r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files\Git\bin\git.exe"):
        if Path(candidate).exists():
            return candidate
    sys.exit("git.exe not found (checked PATH and the default Git for Windows install location)")


# Fields the LLM may add/replace on an existing entry when merging; every other
# field on that entry (the Garmin data) must come back unchanged.
MERGEABLE_FIELDS = {"image", "notes", "exercises", "insights", "comparison"}


def match_garmin_workout(date_str, recent_activities, existing_workouts):
    """Pick the workout for `date_str` (local calendar date). Candidates are
    the freshly pulled Garmin activities AND garmin_ entries already stored in
    workouts.json -- an already-stored session must be MERGED into, never
    skipped or duplicated. Longest duration wins; on a tie the existing entry
    wins (so a re-run can never create a second entry for the same activity).
    Returns (workout, is_existing), or (None, False) if nothing matches."""
    existing_ids = {w["activity_id"] for w in existing_workouts}
    candidates = [
        (w, True) for w in existing_workouts
        if w["activity_id"].startswith("garmin_") and w["start_time"][:10] == date_str
    ] + [
        (a, False) for a in recent_activities
        if a["activity_id"] not in existing_ids and a["start_time"][:10] == date_str
    ]
    if not candidates:
        return None, False
    candidates.sort(key=lambda c: (c[0].get("duration_min") or 0, c[1]), reverse=True)
    return candidates[0]


def validate_merge(before, after, activity_id, is_existing):
    """Check the workouts array after the LLM edit. Returns a list of problems
    (empty = fine). Guards "never duplicates, never skips, never clobbers":
    exactly one entry for the activity, entry count grew by 0 (merge) or 1
    (add), no other entry touched, and on a merge the Garmin fields intact."""
    problems = []
    matching = [w for w in after if w.get("activity_id") == activity_id]
    if len(matching) != 1:
        problems.append(f"expected exactly 1 entry for {activity_id}, found {len(matching)}")
    expected_len = len(before) + (0 if is_existing else 1)
    if len(after) != expected_len:
        problems.append(f"entry count {len(after)}, expected {expected_len}")
    before_by_id = {w["activity_id"]: w for w in before}
    after_by_id = {w.get("activity_id"): w for w in after}
    for aid, w in before_by_id.items():
        if aid != activity_id and after_by_id.get(aid) != w:
            problems.append(f"unrelated entry {aid} was modified or removed")
    if is_existing and matching and activity_id in before_by_id:
        orig, new = before_by_id[activity_id], matching[0]
        for k, v in orig.items():
            if k not in MERGEABLE_FIELDS and new.get(k) != v:
                problems.append(f"Garmin field {k!r} of {activity_id} was changed")
    return problems


def run_llm_merge(date_str, pending_entry, garmin_workout, age, is_existing=False):
    """The single LLM call: read photo + any free text, extract details,
    compare to history, write insights, and add ONE new entry to
    workouts.json. Deliberately atomic -- the raw Garmin match is only
    passed inline, not pre-written to workouts.json, so a failed/over-budget
    call leaves workouts.json untouched and the pending entry retryable.
    Budget is enforced by the CLI itself via --max-budget-usd."""
    texts = pending_entry.get("texts", [])
    has_text = bool(texts)
    photo_line = (
        f"Also read the board photo at {pending_entry['photo']} (relative to the project root) for exercises/structure written on the board."
        if pending_entry.get("photo") else "No board photo was sent for this workout."
    )
    age_line = f"The user is {age} years old -- factor this into HR zone/intensity/recovery framing.\n" if age is not None else ""
    garmin_json = json.dumps(garmin_workout, ensure_ascii=False)

    if has_text:
        text_block = "\n".join(texts)
        extraction_instructions = (
            f"The user's free-text message(s) about this session (Hebrew or English):\n{text_block}\n\n"
            "From the photo and text, extract the exercises, weights used, and any "
            "times/results, and add them onto the new entry as an `image` field "
            "(the photo path, if any), a `notes` field (the raw text), and an "
            "`exercises` field (short structured list of {name, weight, result} where "
            "known)."
        )
        insight_instructions = (
            "Write an `insights` field: a list of 3-10 short, concrete strings "
            "summarizing the session (using the weights/results the user reported)."
        )
    else:
        extraction_instructions = (
            "The user did NOT send a results/weights text message for this workout -- "
            "only the board photo (if any) and the Garmin watch data are available. Add "
            "an `image` field (the photo path, if any) and a `notes` field set to "
            "\"no results text provided -- based on watch data only\". Do not invent "
            "weights or results that weren't provided."
        )
        insight_instructions = (
            "Write an `insights` field: a list of 3-10 short, concrete strings covering "
            "the nature of the workout (type/format from the board photo), energy "
            "systems and intensity, how hard it looks relative to a typical session of "
            "this type, pacing and HR behavior across the session, and recovery "
            "considerations -- all based on the Garmin data (duration, avg/max HR, HR "
            "zones, calories, training effect) and the board photo, NOT on invented "
            "results."
        )

    if is_existing:
        intro = (
            f"The \"workouts\" array in {WORKOUTS_PATH} ALREADY has an entry with "
            f"activity_id \"{garmin_workout['activity_id']}\" for this session. UPDATE THAT "
            "ENTRY IN PLACE -- do NOT add a new entry and do NOT change any of its "
            f"existing Garmin fields. Its current contents:\n{garmin_json}\n\n"
            "Only add or replace these fields on it: image, notes, exercises, insights, "
            "comparison (if it already has some of them, merge the new information in "
            "instead of discarding what is there).\n\n"
        )
    else:
        intro = (
            f"Add ONE new entry to the \"workouts\" array in {WORKOUTS_PATH}, keeping "
            f"the array sorted by start_time. Start from this Garmin activity data "
            f"(copy all its fields as-is):\n{garmin_json}\n\n"
        )
    prompt = (
        f"{intro}"
        f"{photo_line}\n\n"
        f"{age_line}"
        f"{extraction_instructions}\n\n"
        "Then look at other entries already in the file with type \"HIIT\" for "
        "similar past sessions (similar exercises, naming, or -- if no results text "
        "was given -- similar structure/duration/HR profile) and add a `comparison` "
        "string noting what to keep and what to improve versus those, if a reasonable "
        "comparison is possible. "
        f"{insight_instructions} "
        "Do not modify any other existing entry. Do not run git commands -- that "
        "happens separately.\n\n"
        "Your final reply (plain text, not JSON) must be ONLY the Telegram-ready "
        "summary message for the user: the insights as short bullet lines, then the "
        "comparison note if any similar past workout was found. Hebrew or English, "
        "matching the language the user wrote in (default to Hebrew if no text was "
        "sent). No preamble."
    )
    claude_exe = str(Path.home() / ".local" / "bin" / "claude.exe")
    return subprocess.run(
        [
            claude_exe, "-p", prompt,
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
            "--max-budget-usd", str(BUDGET_CEILING_USD),
        ],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )


def main():
    env = load_env()
    telegram_token = env.get("TELEGRAM_BOT_TOKEN")
    state_path = ROOT / "data" / "telegram_state.json"
    chat_id = None
    if state_path.exists():
        chat_id = json.loads(state_path.read_text(encoding="utf-8")).get("chat_id")

    # Defensive: /start already clears the pause flag before spawning this
    # script, but if STOP raced in immediately after, honor it silently --
    # no Garmin/LLM calls, no notifications, while paused.
    if PAUSE_FLAG.exists():
        print("Paused -- exiting without doing anything.")
        return

    os.environ["HIIT_AUTOMATED_RUN"] = "1"
    git_exe = find_git_exe()

    pruned = pending_store.prune_stale(days=GARMIN_LOOKBACK_DAYS)
    if pruned:
        print(f"Pruned stale pending date(s) older than {GARMIN_LOOKBACK_DAYS} days: {', '.join(pruned)}")

    dates = pending_store.unanalyzed_dates()
    if not dates:
        send_telegram(telegram_token, chat_id, "אין תמונה או טקסט ממתינים לניתוח כרגע.")
        return

    target_date = dates[-1]
    older_dates = dates[:-1]

    activities, err = garmin_health.fetch_recent_safe(GARMIN_LOOKBACK_DAYS, telegram_token, chat_id, env)
    if err:
        kind, reason = err
        write_last_run("garmin_failure", f"{kind}: {reason}")
        # garmin_health already sent the (throttled) Telegram alert; pending
        # data is untouched, and no LLM call was made.
        return

    store = json.loads(WORKOUTS_PATH.read_text(encoding="utf-8"))
    workouts_before_text = WORKOUTS_PATH.read_text(encoding="utf-8")
    match, is_existing = match_garmin_workout(target_date, activities, store["workouts"])

    if match is None:
        msg = f"\u23f3 \u05e2\u05d3\u05d9\u05d9\u05df \u05d0\u05d9\u05df \u05d0\u05d9\u05de\u05d5\u05df \u05de-Garmin \u05e2\u05d1\u05d5\u05e8 {target_date} (\u05d4\u05e9\u05e2\u05d5\u05df \u05dc\u05d0 \u05e1\u05d5\u05e0\u05db\u05e8\u05df?). \u05d4\u05ea\u05de\u05d5\u05e0\u05d4/\u05d8\u05e7\u05e1\u05d8 \u05e0\u05e9\u05de\u05e8\u05d5 \u05dc\u05d4\u05de\u05e9\u05da."
        send_telegram(telegram_token, chat_id, msg)
        write_last_run("no_match", f"no Garmin match yet for {target_date}")
        return

    entry = pending_store.load()[target_date]
    age = compute_age(env)

    try:
        cr = run_llm_merge(target_date, entry, match, age, is_existing)
    except subprocess.TimeoutExpired:
        send_telegram(telegram_token, chat_id, f"❌ ניתוח {target_date} עבר את זמן הקצובה (10 דקות). נסה שוב עם /start.")
        write_last_run("timeout", f"analysis timed out for {target_date}")
        return

    result = None
    try:
        result = json.loads(cr.stdout)
    except (json.JSONDecodeError, ValueError):
        pass

    cost_usd = result.get("total_cost_usd") if result else None
    is_error = (result or {}).get("is_error") or cr.returncode != 0
    if is_error:
        send_telegram(
            telegram_token, chat_id,
            f"\u274c \u05e0\u05d9\u05ea\u05d5\u05d7 {target_date} \u05e0\u05db\u05e9\u05dc \u05d0\u05d5 \u05d4\u05d2\u05d9\u05e2 \u05dc\u05ea\u05e7\u05e8\u05ea \u05d4\u05ea\u05e7\u05e6\u05d9\u05d1 (${BUDGET_CEILING_USD:.2f}). "
            "\u05d4\u05ea\u05de\u05d5\u05e0\u05d4/\u05d8\u05e7\u05e1\u05d8 \u05e0\u05e9\u05de\u05e8\u05d5, \u05e0\u05e1\u05d4 \u05e9\u05d5\u05d1 \u05e2\u05dd /start.\n"
            f"(stderr: {cr.stderr[-300:]})",
        )
        write_last_run("over_budget_or_error", f"analysis failed for {target_date}", cost_usd=cost_usd)
        # workouts.json is untouched on failure (the LLM does the add
        # atomically) -- pending photo/text stays queued for a retry.
        return

    problems = validate_merge(
        store["workouts"], json.loads(WORKOUTS_PATH.read_text(encoding="utf-8"))["workouts"],
        match["activity_id"], is_existing,
    )
    if problems:
        WORKOUTS_PATH.write_text(workouts_before_text, encoding="utf-8")  # roll back the bad edit
        send_telegram(
            telegram_token, chat_id,
            f"❌ ניתוח {target_date}: עדכון workouts.json לא עבר בדיקה ובוטל. "
            "התמונה/טקסט נשמרו, נסה שוב עם /start.",
        )
        write_last_run("invalid_merge", f"rolled back {target_date}: {'; '.join(problems)}", cost_usd=cost_usd)
        return

    subprocess.run([sys.executable, str(ROOT / "update_log.py")], cwd=ROOT)

    pushed = False
    subprocess.run([git_exe, "add", "-A"], cwd=ROOT)
    diff_check = subprocess.run([git_exe, "diff", "--cached", "--quiet"], cwd=ROOT)
    if diff_check.returncode != 0:
        commit_msg = f"Analyze workout {target_date}\n\nCo-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
        subprocess.run([git_exe, "commit", "-m", commit_msg], cwd=ROOT)
        push_r = subprocess.run([git_exe, "push"], cwd=ROOT, capture_output=True, text=True)
        pushed = push_r.returncode == 0
        if not pushed:
            print(push_r.stderr)

    pending_store.remove(target_date)

    summary = (result or {}).get("result", "").strip() or "(\u05dc\u05d0 \u05d4\u05ea\u05e7\u05d1\u05dc \u05e1\u05d9\u05db\u05d5\u05dd \u05de\u05d4-LLM)"
    lines = [summary, ""]
    if age is None and not env.get("BIRTH_DATE"):
        lines.append("\u2139\ufe0f \u05dc\u05dc\u05d0 \u05d4\u05ea\u05d0\u05de\u05d4 \u05dc\u05d2\u05d9\u05dc (BIRTH_DATE \u05dc\u05d0 \u05de\u05d5\u05d2\u05d3\u05e8 \u05d1-.env)")
    if cost_usd is not None:
        lines.append(f"\u05e2\u05dc\u05d5\u05ea \u05d4\u05e8\u05d9\u05e6\u05d4: ${cost_usd:.4f}")
    lines.append("\u2705 workouts.json \u05e2\u05d5\u05d3\u05db\u05df, \u05d4\u05d3\u05e9\u05d1\u05d5\u05e8\u05d3 \u05d9\u05e9\u05e7\u05e3 \u05d0\u05ea \u05d6\u05d4" + ("" if pushed else " (\u05d0\u05d1\u05dc \u05d4-push \u05e0\u05db\u05e9\u05dc, \u05d1\u05d3\u05d5\u05e7 \u05d9\u05d3\u05e0\u05d9\u05ea)"))
    if older_dates:
        lines.append(f"\u05d2\u05dd \u05e2\u05d3\u05d9\u05d9\u05df \u05de\u05de\u05ea\u05d9\u05e0\u05d9\u05dd \u05dc\u05e0\u05d9\u05ea\u05d5\u05d7: {', '.join(older_dates)} (\u05e9\u05dc\u05d7 /start \u05e9\u05d5\u05d1)")
    send_telegram(telegram_token, chat_id, "\n".join(l for l in lines if l))

    write_last_run("ok", f"analyzed {target_date}", cost_usd=cost_usd, new_workouts=0 if is_existing else 1)


if __name__ == "__main__":
    main()
