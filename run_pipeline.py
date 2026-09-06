#!/usr/bin/env python3
"""
The "heavy" pipeline run: Polar sync -> photo matching -> recommendations
(via a scoped Claude Code headless call, the only step that costs money) ->
log regen -> git commit & push -> Telegram completion message.

Invoked either by the boot+5min scheduled task, or on-demand via /run_now
from telegram_bot.py (pass --force to run even while paused).

Usage:
    python run_pipeline.py [--force]
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).parent
ENV_PATH = ROOT / ".env"
PAUSE_FLAG = ROOT / "automation_paused.flag"
LAST_RUN_PATH = ROOT / "data" / "last_run.json"
BUDGET_CEILING_USD = 0.50


def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


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
    }, indent=2))


def run(cmd, cwd=ROOT):
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def find_git_exe():
    """Resolve git.exe by full path rather than relying on PATH -- Windows'
    subprocess executable search uses the CALLING process's own inherited
    PATH, not an `env=` dict passed to subprocess.run, so a stale-PATH shell
    (e.g. one started before Git was installed) can't find a bare "git" no
    matter what env is passed. A full path sidesteps that entirely."""
    import shutil

    found = shutil.which("git")
    if found:
        return found
    for candidate in (r"C:\Program Files\Git\cmd\git.exe", r"C:\Program Files\Git\bin\git.exe"):
        if Path(candidate).exists():
            return candidate
    sys.exit("git.exe not found (checked PATH and the default Git for Windows install location)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Run even if automation_paused.flag is present")
    args = parser.parse_args()

    env = load_env()
    telegram_token = env.get("TELEGRAM_BOT_TOKEN")
    state_path = ROOT / "data" / "telegram_state.json"
    chat_id = None
    if state_path.exists():
        chat_id = json.loads(state_path.read_text()).get("chat_id")

    if PAUSE_FLAG.exists() and not args.force:
        print("Automation is paused (automation_paused.flag present). Skipping.")
        write_last_run("skipped_paused", "Scheduled run skipped: automation is paused.")
        send_telegram(telegram_token, chat_id, "\u23f8\ufe0f \u05d4\u05e8\u05d9\u05e6\u05d4 \u05d4\u05de\u05ea\u05d5\u05d6\u05de\u05e0\u05ea \u05d3\u05d5\u05dc\u05d2\u05d4 (\u05de\u05e6\u05d1 pause)")
        return

    os.environ["HIIT_AUTOMATED_RUN"] = "1"
    git_exe = find_git_exe()

    # 1. Deterministic Polar sync (no AI, no cost).
    r = run([sys.executable, "polar_sync.py", "--data-dir", "./data"])
    print(r.stdout)
    if r.returncode != 0:
        print(r.stderr)
        write_last_run("error", f"polar_sync.py failed: {r.stderr[-500:]}")
        send_telegram(telegram_token, chat_id, "\u274c \u05d4\u05e8\u05d9\u05e6\u05d4 \u05e0\u05db\u05e9\u05dc\u05d4 \u05d1\u05e9\u05dc\u05d1 \u05e1\u05e0\u05db\u05e8\u05d5\u05df Polar. \u05e0\u05d1\u05d3\u05d5\u05e7 \u05d0\u05ea \u05d4-log.")
        return

    new_activity_ids = []
    for line in r.stdout.splitlines():
        if line.startswith("RESULT_JSON:"):
            new_activity_ids = json.loads(line[len("RESULT_JSON:"):]).get("new_activity_ids", [])

    # 2. Deterministic photo matching (no AI, no cost).
    r = run([sys.executable, "match_photos.py", "--data-dir", "./data", "--boards-dir", "./boards"])
    print(r.stdout)

    # 3. The only step that costs money: ask Claude Code to write 2-3 short
    # recommendations for each newly-added workout, scoped tightly so this
    # stays cheap and predictable.
    cost_usd = None
    if new_activity_ids:
        prompt = (
            "In C:\\Users\\gurfi\\hiit-pipeline\\data\\workouts.json, find the workout "
            f"entries whose activity_id is one of: {', '.join(new_activity_ids)}. "
            "For each one, write 2-3 short, concrete, specific recommendations "
            "(based on its own duration/avg_hr/max_hr/HR-zone fields, compared to "
            "typical values for that workout's type elsewhere in the file) and save "
            "them as a `recommendations` array of strings directly on that workout's "
            "entry in workouts.json. Do not touch any other entries. Do not run git "
            "commands -- that happens separately."
        )
        claude_exe = str(Path.home() / ".local" / "bin" / "claude.exe")
        cr = subprocess.run(
            [claude_exe, "-p", prompt, "--output-format", "json", "--permission-mode", "bypassPermissions"],
            cwd=ROOT, capture_output=True, text=True, timeout=600,
        )
        try:
            result = json.loads(cr.stdout)
            cost_usd = result.get("total_cost_usd")
        except (json.JSONDecodeError, ValueError):
            print("Could not parse Claude Code output:", cr.stdout[-1000:], cr.stderr[-1000:])

    # 4. Regenerate the compact local log (no AI, no cost).
    run([sys.executable, "update_log.py", "--data-dir", "./data"])

    # 5. Commit + push (git.exe resolved by full path -- see find_git_exe()).
    subprocess.run([git_exe, "add", "-A"], cwd=ROOT)
    diff_check = subprocess.run([git_exe, "diff", "--cached", "--quiet"], cwd=ROOT)
    pushed = False
    if diff_check.returncode != 0:  # non-zero = there ARE staged changes
        commit_msg = f"Automated sync: {len(new_activity_ids)} new workout(s)\n\nCo-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
        subprocess.run([git_exe, "commit", "-m", commit_msg], cwd=ROOT)
        push_r = subprocess.run([git_exe, "push"], cwd=ROOT, capture_output=True, text=True)
        pushed = push_r.returncode == 0
        if not pushed:
            print(push_r.stderr)

    # 6. Report.
    over_budget = cost_usd is not None and cost_usd >= BUDGET_CEILING_USD
    status = "over_budget" if over_budget else "ok"
    message = (
        f"{len(new_activity_ids)} new workout(s)" if new_activity_ids
        else "No new activities since last run."
    )
    write_last_run(status, message, cost_usd=cost_usd, new_workouts=len(new_activity_ids))

    lines = []
    if new_activity_ids:
        lines.append(f"\u2705 \u05d4\u05d3\u05e9\u05d1\u05d5\u05e8\u05d3 \u05e2\u05d5\u05d3\u05db\u05df \u2014 {len(new_activity_ids)} \u05d0\u05d9\u05de\u05d5\u05e0\u05d9\u05dd \u05d7\u05d3\u05e9\u05d9\u05dd" + (" \u05d5\u05e0\u05d3\u05d7\u05e4\u05d5" if pushed else " (\u05d4-push \u05e0\u05db\u05e9\u05dc, \u05d1\u05d3\u05d5\u05e7 \u05d9\u05d3\u05e0\u05d9\u05ea)"))
    else:
        lines.append("\u2139\ufe0f \u05d0\u05d9\u05df \u05d0\u05d9\u05de\u05d5\u05e0\u05d9\u05dd \u05d7\u05d3\u05e9\u05d9\u05dd \u05de\u05d0\u05d6 \u05d4\u05e8\u05d9\u05e6\u05d4 \u05d4\u05e7\u05d5\u05d3\u05de\u05ea")
    if cost_usd is not None:
        lines.append(f"\u05e2\u05dc\u05d5\u05ea \u05d4\u05e8\u05d9\u05e6\u05d4: ${cost_usd:.4f}")
        if over_budget:
            lines.append(f"\u26a0\ufe0f \u05d7\u05e8\u05d2\u05d4 \u05de\u05ea\u05e7\u05e8\u05ea \u05d4-${BUDGET_CEILING_USD:.2f} \u05dc\u05e8\u05d9\u05e6\u05d4 \u05d0\u05d5\u05d8\u05d5\u05de\u05d8\u05d9\u05ea!")
    send_telegram(telegram_token, chat_id, "\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
