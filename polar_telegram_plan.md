# HIIT Workout Pipeline — Plan for Claude Code (Polar + Telegram)

Status: PLANNING ONLY. Nothing below has been executed. Hand this file to
Claude Code on the laptop to implement.

## 0. Security first (do this before anything else)
- [ ] Revoke the current Telegram bot token via @BotFather (`/revoke`) and get
      a fresh one — the old one was pasted in plaintext in a chat.
- [ ] Store the new token as an environment variable (`TELEGRAM_BOT_TOKEN`),
      never hardcoded in a script or committed to git.
- [ ] Same for Polar `client_id` / `client_secret` once created (step 1).
- [ ] Add a `.env` (or OS-level env vars) + `.gitignore` entry before the repo
      is created — secrets must never reach git history, even in a private repo.
- [ ] **Set up cost guardrails before doing any real work in Claude Code:**
      - Install `ccusage` (github.com/ryoppippi/ccusage) for visibility into
        session/daily costs.
      - Add a `Stop` hook in `~/.claude/settings.json` that runs a
        budget-check command after every session and prints a warning —
        e.g. via the `claude-cost` skill's `check-budget`, or an equivalent
        `ccusage`-based check — configured with:
        - **Setup-session ceiling: $15.** If the initial setup session
          (repo creation, backfill parsing, OAuth wiring, dashboard build)
          is trending toward this, stop and check in with the user before
          continuing rather than pushing through.
        - **Per-run ceiling: $0.50** for every recurring automated sync run
          (the Task Scheduler → Claude Code headless call). This should be
          a hard budget in the hook config, not just a monitoring number —
          if a routine run is approaching or exceeding $0.50, that's a
          signal something is wrong (retry loop, unexpectedly large diff,
          etc.), so the run should log the issue and stop rather than keep
          spending unsupervised.
      - Since the recurring run is unattended (no one watching in real
        time), prefer failing loudly (log file entry, and/or a Telegram
        message via the bot itself) over failing silently when a budget
        cap is hit.

### 0a. One-time Garmin Connect historical backfill (separate from the ongoing Polar pipeline)
Since the ongoing pipeline moves to Polar, this step exists only to seed the
dashboard with existing history so it's not empty on day one, and to have
real historical data to draw insights from.

- Use the existing `garmin_sync.py --full-history` (already built) to pull
  everything from the Garmin Connect account into its own
  `data/workouts_garmin_backfill.json`.
  **UPDATE:** the user already has (or will have) Garmin's official
  "Export Your Data" ZIP sitting in `Downloads` — this contains JSON
  metadata files plus per-activity `.fit` files (typically under a path
  like `DI_CONNECT/DI-Connect-Fitness/...`). Prefer parsing this local
  export directly over the API-based approach:
  - No `garminconnect` login/API dependency needed for the backfill at all
    — just unzip and glob for `**/*.fit`.
  - Reuse the existing `fitparse`-based session/HR-zone logic unchanged —
    only the input source changes (bytes from a local file instead of
    `download_activity()`).
  - Cross-reference the accompanying JSON files for metadata the FIT file
    may not carry well (e.g. a user-entered activity name/description).
  - `garmin_sync.py` should get a new mode, e.g.
    `garmin_sync.py --from-export-zip <path-to-zip>`, instead of relying on
    `--full-history` (API-based) for this step.
  - The export ZIP will include non-HIIT activities too (e.g. runs) mixed
    in with the CrossFit sessions — read each FIT's `session` message
    `sport`/`sub_sport` fields and map them into the same `type` field
    used for the Polar pipeline (section 4). Historical runs get logged
    the same way as future Polar runs: included in `workouts.json`, no
    board photo expected, kept segmented from HIIT in dashboard charts and
    summaries rather than blended into one average.
- This is a **one-time, read-only analysis pass** — it does not become part
  of the recurring automation (step 5 only talks to Polar + Telegram going
  forward). No need to keep the Garmin login working long-term after this.
- Merge the backfilled entries into the same `workouts.json` the dashboard
  reads, tagging each with `source: "garmin"` (vs `source: "polar"` for
  everything from here on) — so it's clear which pipeline produced which
  entry, in case the two ever need to be reconciled or compared.
- No board photos are expected for this historical batch (no Telegram bot
  existed back then) — those entries simply have no `image` field, same as
  any other unmatched workout.
- Claude Code should also produce a short one-off written summary/insights
  from this backfilled history once it's in `workouts.json` (trends,
  volume, typical zone distribution) — this is the "content to talk about"
  for the dashboard's initial state.

## 1. Data source: Polar AccessLink API (official, OAuth2, read-only)
Replaces the earlier Garmin-unofficial-library approach — this one is
Polar's supported, documented API.

- Register a client at https://admin.polaraccesslink.com (needs a Polar Flow
  login — same account as the Polar Flow app on the phone) → get
  `client_id` + `client_secret`.
- OAuth2 authorization: one-time browser flow to link the account, yields a
  long-lived `access_token` + `x-user-id`. Store both as env vars /
  local secrets file (gitignored).
- Data model is **transaction-based**, not a simple date-range GET:
  1. `POST /v3/users/{user-id}/exercise-transactions` → opens a transaction,
     returns a `transaction-id` (only if new data exists since last fetch).
  2. `GET .../exercise-transactions/{id}/exercises` → list of new exercise
     summaries (duration, calories, HR avg/max, sport, start time, HR zone
     durations are typically included in the summary — no manual FIT-zone
     math needed like the Garmin path required).
  3. `GET .../exercises/{id}` (and optionally `/gpx`, `/tcx`, `/samples/...`)
     for per-exercise detail if the zone breakdown isn't already in the
     summary.
  4. `PUT .../exercise-transactions/{id}` → **commit** the transaction. Until
     committed, the same data will be re-returned next time — don't commit
     until the exercise has been successfully written to `workouts.json`.
- Reference client library: `polar-accesslink` on PyPI (community-packaged
  version of Polar's own example client) — saves hand-rolling the OAuth +
  transaction flow.

## 2. Photo capture: Telegram bot (`my_wod_telegram_bot`)
Replaces the earlier "synced photos folder" idea — sending the WOD board
photo to the bot from the phone right after training is simpler and needs no
iCloud/Google Photos sync setup.

- Poll with the Bot API's `getUpdates` (long-polling, no public webhook/
  hosting needed since this runs from the same on-demand script on the
  laptop): `https://api.telegram.org/bot<TOKEN>/getUpdates?offset=<last_update_id+1>`
- For each new message with a photo: `getFile` → download the largest size →
  save to `boards/<message-date>.jpg`.
- Track `last_update_id` locally (small state file) so re-runs don't
  re-download the same photos.
- Optional nice-to-have: have the bot reply with a ✅ once the photo is
  matched to a workout, so there's confirmation on the phone.

## 3. Matching photo → workout
Match by **calendar date**, not closest timestamp — the board photo may be
uploaded hours after the workout (or the next morning), so date-matching is
more robust than time-proximity.

- **Timezone**: Telegram message `date` is UTC; Polar exercise `start_time`
  is local (Israel). Convert both to the same local timezone before
  comparing calendar dates — otherwise a late-night workout can land on the
  wrong day.
- **One workout, one photo (common case)**: match directly.
- **Multiple Polar exercises same date**: prefer the longest-duration one,
  or the one whose sport type is closest to HIIT/CrossFit; if still
  ambiguous, leave unmatched and flag for manual review rather than
  guessing.
- **Multiple photos same date**: the most recently sent photo wins.
- **No matching exercise for a photo's date** (forgot to track / sync
  failed): keep the photo in a "pending" state, don't force a match.
- Write the matched `image: "boards/<date>.jpg"` path into that workout's
  entry once resolved.

## 4. Data + memory + dashboard (unchanged from before)
- Merge into `data/workouts.json` (schema stays close to what's already
  built — adjust field names to whatever the Polar exercise summary actually
  returns, e.g. `heartRateZones` instead of manually computed `z1_min..z5_min`).
- Every entry carries a `type` field taken directly from Polar's own sport
  classification (e.g. `CROSSFIT`/`OTHER_INDOOR` vs `RUNNING`) — the
  pipeline does not assume everything is HIIT. ~95%+ will be HIIT, but
  occasional other activity types (a run, etc.) are logged the same way,
  just without an expected board photo (no photo match attempted for
  non-HIIT types).
- Update `/areas/hiit-workout-log.md` in memory with one compact line per new
  workout (same format as before, `type` included). If non-HIIT entries
  become frequent rather than occasional, consider splitting into a
  separate log file at that point — not needed now.
- Regenerate `dashboard.html`: filter/segment by `type` rather than
  averaging everything together — HR zone distribution for a run looks very
  different from a CrossFit session, so mixing them would distort the
  trend charts. Weekly summaries should report activity types separately
  (e.g. "3 HIIT sessions, 1 run") rather than one blended average.
- `git commit` + `push` → GitHub Pages serves the updated dashboard.

## 5. Automation (unchanged)
- Windows Task Scheduler: trigger at logon + 5 min delay.
- Runs a script that invokes Claude Code headlessly to execute steps 1–4 in
  order, then exits.
- **End-of-run Telegram notification** (sent via the same bot, to the same
  chat used for board photos): after every routine run, send one message
  reporting:
  1. **What this run cost** — pull the actual figure from `ccusage`/the
     budget-check hook set up in step 0 (don't estimate separately; reuse
     the same number the cost guardrail already computed).
  2. **Whether the dashboard was updated** — confirm the `git push`
     succeeded (e.g. "Dashboard updated — 2 new workouts" or "No new
     activities since last run" if nothing changed).
  - If the run failed or hit the $0.50 budget cap (per step 0), the
    notification should say so plainly instead of reporting a clean
    success — this doubles as the "fail loudly" mechanism already planned
    for the budget guardrail.

## Open questions for the Claude Code session to resolve by testing
- Does the Polar exercise summary already include per-zone time (likely
  yes — Polar devices compute this on-device), or is a `/samples` call with
  raw HR series + manual zone math needed (as we did for Garmin)?
- Confirm the exact field names in a real API response (docs alone don't
  always match reality) before finalizing the `workouts.json` schema.
