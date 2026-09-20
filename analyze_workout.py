#!/usr/bin/env python3
"""
The /start-triggered analysis run. Processes the pending submission with the
MOST RECENT workout date -- older ones stay queued and are just mentioned.
The workout date comes from the CONTENT, never from when it was sent:
  1. Archive entries that can never be handled (workout date older than 30
     days, or undated for 30 days after the message).
  2. Date each pending entry: explicit date in the text (regex, done at intake
     by telegram_bot.py), else a date visible on the photo (one tiny vision
     call, max $0.05, read once per photo), else NOT guessed -- ask the user
     once on Telegram and keep the entry queued until they reply.
  3. Take up to 3 workout dates, newest first; each is its own run (4-6).
     Garmin is pulled once via garmin_health (one retry, failure
     classification, Telegram alert on failure -- deterministic, no LLM),
     looking back 7 days, or further (max 30) if an older date needs it.
  4. Match that workout date (local calendar date; longest duration wins on
     a multi-match day; an already-stored entry is merged into). If there is
     no Garmin match yet, tell the user and leave the entry queued.
  5. If matched, make ONE Claude Code headless call (the only step that costs
     real money) to read the board photo + any free text, extract details,
     compare against similar past workouts in workouts.json, write 3-10 short
     insights, and add (or update) ONE entry in workouts.json.
  6. Regenerate hiit-workout-log.md, commit + push, send one Telegram summary,
     and clear the pending entries. Dates beyond the 3rd are listed in the
     last reply and stay queued for the next /start.

Hard budget cap: $0.50 PER RUN (per workout date). The vision date read
(<= $0.05) is deducted from that run's --max-budget-usd, which the CLI
enforces itself. If it trips, we fail loudly via Telegram and keep the
entries queued.

Invoked by telegram_bot.py when it sees /start or "start" (case-insensitive).
Usage: python analyze_workout.py
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests

import env_setup

env_setup.load()  # populate os.environ from .env before anything reads GARMIN_*/TELEGRAM_*/BIRTH_DATE

import garmin_health
import pending_store
import workout_date

ROOT = Path(__file__).parent
ENV_PATH = ROOT / ".env"
PAUSE_FLAG = ROOT / "automation_paused.flag"
LAST_RUN_PATH = ROOT / "data" / "last_run.json"
WORKOUTS_PATH = ROOT / "data" / "workouts.json"
LOCK_PATH = ROOT / "data" / "analyze.lock"  # gitignored: exists while a run is active
LOCK_STALE_S = 30 * 60  # a lock older than this is from a crashed run
BUDGET_CEILING_USD = 0.50
GARMIN_LOOKBACK_DAYS = 7  # the plain automatic sync stays at 7 days
MAX_WORKOUT_AGE_DAYS = workout_date.MAX_AGE_DAYS  # /start widens the lookback this far for older explicit dates
MAX_RUNS_PER_START = 3  # workout dates processed per /start, newest first
VISION_BUDGET_USD = 0.05  # cap for the photo-date read
MAX_VISION_CALLS = 3  # per run
CLAUDE_EXE = str(Path.home() / ".local" / "bin" / "claude.exe")


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
        print(f"  (failed to send Telegram message: {type(e).__name__})")  # str(e) embeds the bot-token URL


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


def read_photo_date(photo_rel, ref_day):
    """Tiny, tightly capped vision call: the workout date printed on the photo
    (board / app screenshot), or None. Returns (date_or_None, cost_usd, ok);
    ok=False means the call failed and may be retried on a later /start."""
    prompt = (
        f"Look at the image at {photo_rel} (relative to the project root). If it visibly shows a "
        "workout date (a board or an app screenshot), reply with ONLY that date as YYYY-MM-DD. "
        "Dates on it are day-first (DD/MM/YY). If no date is visible, reply with ONLY: null"
    )
    try:
        cr = subprocess.run(
            [
                CLAUDE_EXE, "-p", prompt, "--output-format", "json", "--model", "haiku",
                "--allowedTools", "Read", "--max-budget-usd", str(VISION_BUDGET_USD),
            ],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
        )
        result = json.loads(cr.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError, OSError):
        return None, 0.0, False
    cost = result.get("total_cost_usd") or 0.0
    if result.get("is_error") or cr.returncode != 0:
        return None, cost, False
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", (result.get("result") or "").strip())
    try:
        found = date(int(m[1]), int(m[2]), int(m[3])) if m else None
    except ValueError:
        found = None
    if found and not workout_date.is_plausible(found, ref_day):
        found = None
    return found, cost, True


def resolve_photo_dates():
    """Vision date read for undated entries whose photo was not read yet.
    Returns {entry_id: cost_usd} for the reads made."""
    costs = {}
    for eid, e in pending_store.entries():
        if e["workout_date"] or not e["photo"] or e["photo_date_checked"] or len(costs) >= MAX_VISION_CALLS:
            continue
        found, cost, ok = read_photo_date(e["photo"], pending_store.sent_local_date(e))
        costs[eid] = cost
        if found:
            pending_store.update(eid, workout_date=found.isoformat(), date_source="photo", photo_date_checked=True)
        elif ok:
            pending_store.update(eid, photo_date_checked=True)
    return costs


def run_llm_merge(date_str, pending_entry, garmin_workout, age, is_existing=False, budget_usd=BUDGET_CEILING_USD):
    """The single LLM call: read photo + any free text, extract details,
    compare to history, write insights, and add ONE new entry to
    workouts.json. Deliberately atomic -- the raw Garmin match is only
    passed inline, not pre-written to workouts.json, so a failed/over-budget
    call leaves workouts.json untouched and the pending entry retryable.
    Budget (what is left of the per-run cap) is enforced by the CLI itself."""
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
    return subprocess.run(
        [
            CLAUDE_EXE, "-p", prompt,
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
            "--max-budget-usd", str(budget_usd),
        ],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )


SOURCE_HE = {"text": "מהטקסט", "photo": "מהתמונה", "user_reply": "לפי התשובה שלך"}


def fmt_date(iso):
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m")


def entry_label(e):
    if e["workout_date"]:
        return fmt_date(e["workout_date"])
    return "נשלח " + datetime.fromisoformat(e["sent_at"]).strftime("%d/%m %H:%M")


def process_one(target_date, group, vision_cost, activities, env, send, git_exe):
    """One workout date = one run with its own $0.50 cap (minus its vision read).
    `group` is the pending [(id, entry)] for that date; `send(text)` posts to Telegram.
    Returns {"status", "message", "cost", "new"} for the last_run summary."""
    store = json.loads(WORKOUTS_PATH.read_text(encoding="utf-8"))
    workouts_before_text = WORKOUTS_PATH.read_text(encoding="utf-8")
    match, is_existing = match_garmin_workout(target_date, activities, store["workouts"])

    if match is None:
        send(f"⏳ עדיין אין אימון מ-Garmin עבור {target_date} (השעון לא סונכרן?). התמונה/טקסט נשמרו להמשך.")
        return {"status": "no_match", "message": f"no Garmin match yet for {target_date}", "cost": vision_cost, "new": 0}

    photo_id = next((k for k, e in reversed(group) if e["photo"]), None)  # latest photo wins
    entry = {
        "photo": pending_store.finalize_photo(photo_id, target_date) if photo_id else None,
        "texts": [t for _, e in group for t in e["texts"]],
    }
    date_source = group[0][1]["date_source"]
    age = compute_age(env)
    merge_budget = round(BUDGET_CEILING_USD - vision_cost, 4)  # this run stays within its own cap

    try:
        cr = run_llm_merge(target_date, entry, match, age, is_existing, merge_budget)
    except subprocess.TimeoutExpired:
        send(f"❌ ניתוח {target_date} עבר את זמן הקצובה (10 דקות). נסה שוב עם /start.")
        return {"status": "timeout", "message": f"analysis timed out for {target_date}", "cost": vision_cost, "new": 0}

    result = None
    try:
        result = json.loads(cr.stdout)
    except (json.JSONDecodeError, ValueError):
        pass

    merge_cost = result.get("total_cost_usd") if result else None
    cost_usd = None if merge_cost is None and not vision_cost else round((merge_cost or 0) + vision_cost, 4)
    is_error = (result or {}).get("is_error") or cr.returncode != 0
    if is_error:
        send(
            f"❌ ניתוח {target_date} נכשל או הגיע לתקרת התקציב (${BUDGET_CEILING_USD:.2f}). "
            "התמונה/טקסט נשמרו, נסה שוב עם /start.\n"
            f"(stderr: {cr.stderr[-300:]})"
        )
        # workouts.json is untouched on failure (the LLM does the add
        # atomically) -- pending photo/text stays queued for a retry.
        return {"status": "over_budget_or_error", "message": f"analysis failed for {target_date}", "cost": cost_usd, "new": 0}

    problems = validate_merge(
        store["workouts"], json.loads(WORKOUTS_PATH.read_text(encoding="utf-8"))["workouts"],
        match["activity_id"], is_existing,
    )
    if problems:
        WORKOUTS_PATH.write_text(workouts_before_text, encoding="utf-8")  # roll back the bad edit
        send(f"❌ ניתוח {target_date}: עדכון workouts.json לא עבר בדיקה ובוטל. התמונה/טקסט נשמרו, נסה שוב עם /start.")
        return {"status": "invalid_merge", "message": f"rolled back {target_date}: {'; '.join(problems)}", "cost": cost_usd, "new": 0}

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

    for k, _ in group:
        pending_store.remove(k)

    summary = (result or {}).get("result", "").strip() or "(לא התקבל סיכום מה-LLM)"
    lines = [summary, ""]
    lines.append(f"📅 תאריך האימון: {fmt_date(target_date)} ({SOURCE_HE.get(date_source, '')})")
    if age is None and not env.get("BIRTH_DATE"):
        lines.append("ℹ️ ללא התאמה לגיל (BIRTH_DATE לא מוגדר ב-.env)")
    if cost_usd is not None:
        lines.append(f"עלות הריצה: ${cost_usd:.4f}")
    lines.append("✅ workouts.json עודכן, הדשבורד ישקף את זה" + ("" if pushed else " (אבל ה-push נכשל, בדוק ידנית)"))
    send("\n".join(l for l in lines if l))
    return {"status": "ok", "message": f"analyzed {target_date}", "cost": cost_usd, "new": 0 if is_existing else 1}


def acquire_run_lock():
    """True if this process now owns the run lock, False if another run is active."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - LOCK_PATH.stat().st_mtime < LOCK_STALE_S:
                    return False
                LOCK_PATH.unlink()  # stale: the previous run crashed
            except FileNotFoundError:
                pass  # released in the meantime, retry
            continue
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True
    return False


def main():
    env_setup.configure_stdio()
    # Two /start in one poll (or overlapping polls) must not run two paid analyses.
    if not acquire_run_lock():
        print("Another analysis run is active -- exiting.")
        return
    try:
        _main()
    finally:
        LOCK_PATH.unlink(missing_ok=True)


def _main():
    env = load_env()
    telegram_token = env.get("TELEGRAM_BOT_TOKEN")
    state_path = ROOT / "data" / "telegram_state.json"
    chat_id = None
    if state_path.exists():
        chat_id = json.loads(state_path.read_text(encoding="utf-8")).get("chat_id")

    # Defensive: /start already clears the pause flag before spawning this
    # script, but if STOP raced in immediately after, honor it silently --
    # no Garmin/LLM/vision calls, no notifications, while paused.
    if PAUSE_FLAG.exists():
        print("Paused -- exiting without doing anything.")
        return

    os.environ["HIIT_AUTOMATED_RUN"] = "1"
    git_exe = find_git_exe()

    pruned = pending_store.prune_stale()
    if pruned:
        labels = ", ".join(entry_label(e) for _, e in pruned)
        print(f"Archived stale pending entries: {labels}")
        send_telegram(telegram_token, chat_id, f"🗄️ הועברו לארכיון (אימון ישן מ-{MAX_WORKOUT_AGE_DAYS} יום, או ללא תאריך כל הזמן הזה): {labels}")

    if not pending_store.entries():
        send_telegram(telegram_token, chat_id, "אין תמונה או טקסט ממתינים לניתוח כרגע.")
        return

    vision_costs = resolve_photo_dates()
    entries = pending_store.entries()
    undated = [(k, e) for k, e in entries if not e["workout_date"]]
    dated = [(k, e) for k, e in entries if e["workout_date"]]

    if undated:
        # Never guess a date: ask once, keep it queued until the user replies.
        eid, e = undated[0]
        sent = datetime.fromisoformat(e["sent_at"]).strftime("%d/%m %H:%M")
        send_telegram(
            telegram_token, chat_id,
            f"📅 לא מצאתי תאריך אימון בתמונה או בטקסט ששלחת ב-{sent}. "
            "שלח את תאריך האימון (למשל 19/9 או 'אתמול') ואז /start.",
        )
        pending_store.update(eid, asked_date=True)
    if not dated:
        write_last_run(
            "awaiting_date", f"asked for the workout date of the entry sent {entry_label(undated[0][1])}",
            cost_usd=round(sum(vision_costs.values()), 4) or None,
        )
        return

    # Newest workout date first; each date is its own run (its own $0.50 cap).
    dates = sorted({e["workout_date"] for _, e in dated}, reverse=True)
    targets, remaining = dates[:MAX_RUNS_PER_START], dates[MAX_RUNS_PER_START:]
    groups = {d: [(k, e) for k, e in dated if e["workout_date"] == d] for d in targets}

    tail_lines = []
    if remaining:
        tail_lines.append(f"עדיין ממתינים לניתוח: {', '.join(fmt_date(d) for d in remaining)} (שלח /start שוב)")
    if undated:
        tail_lines.append(f"ממתינים ללא תאריך אימון: {len(undated)}")
    tail = "\n" + "\n".join(tail_lines) if tail_lines else ""

    # One Garmin pull: 7 days, or further back (max 30) when an older date needs it.
    oldest_age = (datetime.now(workout_date.LOCAL_TZ).date() - datetime.strptime(targets[-1], "%Y-%m-%d").date()).days
    lookback = min(MAX_WORKOUT_AGE_DAYS + 1, max(GARMIN_LOOKBACK_DAYS, oldest_age + 1))
    known_ids = {w["activity_id"] for w in json.loads(WORKOUTS_PATH.read_text(encoding="utf-8"))["workouts"]}
    activities, err = garmin_health.fetch_recent_safe(
        lookback, telegram_token, chat_id, env, known_ids=known_ids, only_dates=set(targets),
    )
    if err:
        kind, reason = err
        write_last_run("garmin_failure", f"{kind}: {reason}", cost_usd=round(sum(vision_costs.values()), 4) or None)
        # garmin_health already sent the (throttled) Telegram alert; pending
        # data is untouched, and no main LLM call was made.
        return

    results = []
    for i, d in enumerate(targets):
        is_last = i == len(targets) - 1
        send = lambda text, is_last=is_last: send_telegram(telegram_token, chat_id, text + (tail if is_last else ""))
        vision_cost = sum(vision_costs.get(k, 0.0) for k, _ in groups[d])
        results.append(process_one(d, groups[d], vision_cost, activities, env, send, git_exe))

    statuses = {r["status"] for r in results}
    processed_ids = {k for d in targets for k, _ in groups[d]}
    unattributed = sum(c for k, c in vision_costs.items() if k not in processed_ids)  # reads for entries not run now
    total = round(sum(r["cost"] or 0 for r in results) + unattributed, 4)
    write_last_run(
        statuses.pop() if len(statuses) == 1 else "partial",
        "; ".join(r["message"] for r in results),
        cost_usd=total or None,
        new_workouts=sum(r["new"] for r in results),
    )


if __name__ == "__main__":
    main()
