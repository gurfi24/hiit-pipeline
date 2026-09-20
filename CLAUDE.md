# hiit-pipeline

Personal automation: log a HIIT workout on a Garmin watch, send a board photo
+ free-text recap to a Telegram bot, get one AI-written insights message back.
Garmin + Telegram only -- Polar was fully removed (2026-09-19).

**Repo is PUBLIC on GitHub** (`gurfi24/hiit-pipeline`). Never commit `.env` or
anything containing `GARMIN_PASSWORD`/`GITHUB_TOKEN`/`TELEGRAM_BOT_TOKEN`/
`BIRTH_DATE`. `data/workouts.json` and `boards/*.jpg` (board photos) are
already tracked/public by the user's own established choice -- don't add new
personal files to git without asking.

## Flow

1. Garmin watch syncs a workout to Garmin Connect.
2. User sends the bot a board photo, then (optionally) a free-text recap --
   possibly days after the workout.
3. User sends `/start` (or plain "start", case-insensitive) -> `analyze_workout.py`
   runs up to 3 pending *workout dates* per `/start`, newest first; each is
   its own run: match it against Garmin activity (an already-stored `garmin_`
   entry is merged into, never skipped or duplicated), ONE LLM call capped at
   $0.50 to extract details + write insights + merge into `workouts.json`,
   commit + push, one Telegram summary. Dates beyond the 3rd are listed in
   the last reply and stay queued. Only one analysis runs at a time
   (`data/analyze.lock`, stale after 30 min); several `/start` in one poll
   spawn one run, and a second run while one is active exits silently.
4. `/stop`/"STOP" creates `automation_paused.flag`: no Garmin/LLM/vision calls
   and no notifications until the next `/start`. The poller keeps running as
   the minimal listener: photos and texts sent while paused are still SAVED
   to pending, silently, so nothing is lost; only `/start` (or "start")
   removes the flag and processes them. `/status` is ignored while paused.

**Scheduling:** Task `HIIT-Pipeline-Poller` runs hourly at :30
(`StartWhenAvailable`, 3650-day repetition, as user `gurfi`, interactive
logon) via `scripts/run_task.cmd telegram_bot.py`. Register or re-register it
with `scripts\setup_scheduled_tasks.ps1` (there is no HeavyRun task). The
launcher sets absolute python/git/claude PATH and `PYTHONIOENCODING=utf-8`,
cds to the project and logs to `logs/telegram_bot.log` (gitignored). An idle
poll makes no Garmin/LLM/vision call and costs $0. A `/start` is therefore
processed within the hour.

**Workout date = from the content, never the send time.** Priority: (1) an
explicit date in the text (`workout_date.py`, plain regex, no LLM), (2) a date
visible on the photo (one separate vision call, max $0.05, read once per
photo), (3) neither -> never guess and never fall back to the send date: ask
ONE Telegram question and keep it queued; the user's short date reply
(`19/9`, `yesterday`, `אתמול`) dates it, then `/start` again.

**Windows:** an explicit workout date is valid up to 30 days back. `/start`
looks back 7 days on Garmin, or further (max 30) when an older date needs it.
Dated entries are archived (and announced on Telegram) only once the workout
is older than 30 days; undated ones 30 days after the message date. The plain
automatic sync (`garmin_sync.py` CLI) keeps its 7-day limit.

## Key files

- `telegram_bot.py` -- scheduled poller (Task Scheduler, hourly :30). No AI,
  no Garmin calls except spawning `analyze_workout.py` on `/start` (once per
  batch). Writes `data/poller_heartbeat.json` every poll; `/status` shows it
  ("poller alive"), pause state, last run, pending dates and Garmin state.
- `scripts/telegram_pending_check.py` -- read-only look at the Telegram queue
  (getUpdates, no offset, nothing consumed or written; prints counts/types/
  times only, never text or file ids). Don't run it while the poller runs.
- `scripts/run_task.cmd`, `scripts/setup_scheduled_tasks.ps1` -- task launcher
  and registration (see Scheduling).
- `analyze_workout.py` -- the `/start` handler. Only place the LLM is called.
- `workout_date.py` -- pure regex date extractor (DD/MM[/YY], DD.MM.YY,
  "19 Sept", Hebrew month names, yesterday/אתמול, day before yesterday/שלשום).
  Bare `DD.MM` is not accepted (collides with weights like `18.5 kg`).
- `pending_store.py` -- `data/pending.json`: one entry per submission
  (photo + texts sent within 2h of each other), with `workout_date` and
  `date_source`. `prune_stale()` archives dated entries whose workout is older
  than 30 days (undated: 30 days after the message) before any Garmin/LLM call.
- `garmin_sync.py` -- Garmin API wrapper (unofficial `garminconnect` lib).
  `get_client()` handles login/MFA/token caching. `fetch_recent()` is the
  bounded on-demand pull `analyze_workout.py` uses. A **direct CLI run**
  (`python garmin_sync.py --data-dir ./data`) is **always bounded to 7 days**
  regardless of `last_synced` -- `--full-history` is an explicit unbounded
  escape hatch, `--from-export-zip` is a one-time bulk backfill with no live
  login. `--dry-run` fetches and prints without writing.
- `garmin_health.py` -- every Garmin login attempt from every entry point
  goes through here: checks a rate-limit cooldown first (refuses to even try
  if one is active -- protects against extending a 429 block by retrying
  manually), one retry for transient failures, classifies the rest
  (mfa/login/token/rate_limit/network/empty_response/unknown), tracks state
  in `data/garmin_state.json`, sends a throttled (max 1/6h) Telegram alert.
- `scripts/garmin_login.py` -- manual interactive re-auth entry point
  (prompts for MFA if needed). Also goes through `garmin_health.py`.
- `env_setup.py` -- loads `.env` into `os.environ` (via `python-dotenv`, or a
  fallback parser), path resolved relative to itself (project root), not
  cwd. Every entry script calls `env_setup.load()` at startup.
- `update_log.py` -- regenerates `hiit-workout-log.md` from `workouts.json`.
- `dashboard.html` -- static page, fetches `data/workouts.json` client-side.

## Gotchas

- **Console encoding**: a Windows console/pipe is cp1252, so `print()` of
  Hebrew/emoji raises. Every entry script's `main()` starts with
  `env_setup.configure_stdio()` (UTF-8, `errors="replace"`, exports
  `PYTHONIOENCODING=utf-8` to children); the bot also passes it to the spawned
  `analyze_workout.py`. New entry scripts must do the same.
- **Never print exception strings from `requests`**: they embed the bot-token
  URL. Print `type(e).__name__` (see `send_telegram`).
- **`analyze_workout.py` does `git add -A`**: any uncommitted file in the repo
  is committed and pushed with the next analysis. Commit your own changes
  first, and keep runtime files (`logs/`, locks, heartbeat) gitignored.
- **Windows encoding**: `Path.write_text()`/`read_text()` default to the
  locale codepage (cp1252 here), not UTF-8 -- crashes on Hebrew text. Always
  pass `encoding="utf-8"` explicitly on both read and write for any JSON
  file in this project.
- **Activity ID format**: always `garmin_<activityId>` (matches the
  export-zip backfill's convention). The live-API path
  (`_build_workout_entry` in `garmin_sync.py`) must keep this prefix or
  dedup against the existing DB silently breaks.
- **Never call `return_on_mfa=True`** on a `Garmin(...)` instance and then
  use the client for anything else -- that mode skips profile loading
  entirely, so `client.display_name` stays unset and every later API call
  fails with "Display name is not set" even after a technically-successful
  login. Use a `prompt_mfa` callable instead (see `get_client()`).
- **garminconnect wraps almost every exception** from its `login()` into a
  generic `GarminConnectConnectionError`, but preserves the real cause via
  `raise ... from e` (`exc.__cause__`). `garmin_sync._unwrap()` and
  `garmin_health.classify_error()` both unwrap this -- don't classify a raw
  `str(exc)`/`type(exc)` without checking `__cause__` first.
- Don't call `garmin_sync.get_client()` directly from application code --
  always go through `garmin_health.py` (cooldown check + retry +
  classification + notification). `garmin_sync.py` itself only calls it
  directly inside its own CLI `main()`, via `garmin_health.login_only_safe()`.

## Env vars (`.env`, gitignored)

`TELEGRAM_BOT_TOKEN`, `GITHUB_TOKEN` (git push credential helper),
`GARMIN_EMAIL`, `GARMIN_PASSWORD` (only needed for the first login --
session then caches to `~/.garminconnect`), `BIRTH_DATE` (optional,
`YYYY-MM-DD` -- only the computed integer age is ever passed to the LLM or
logged, never the birth date itself).

## Budget

Each analysis run (one workout date; up to 3 per `/start`) is hard-capped at
$0.50 via `--max-budget-usd` on the `claude -p` call in `analyze_workout.py`
(enforced by the CLI, not just monitored). The optional photo-date read (max
$0.05, Haiku, Read tool only) is deducted from its own run's budget. A
failed/over-budget run leaves `workouts.json` untouched (the
LLM adds the whole merged entry atomically) and the pending photo/text stays
queued for a retry.

## Tests

`python -m unittest discover -s tests` -- no network, no LLM, no Garmin. Must
pass with the default Windows encoding (never set `PYTHONIOENCODING` by hand;
`python -m unittest tests.test_x` fails on imports -- use discovery). Every
test module imports `tests/_guard.py`, which forces all data paths into a temp
dir and raises `RealDataAccessError` (a `BaseException`) if anything touches
the real `data/`, `boards/` or `automation_paused.flag`. Never write tests
that read or write those directly.
