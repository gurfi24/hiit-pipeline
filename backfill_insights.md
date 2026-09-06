# Garmin Backfill — Initial Insights (one-time, step 0a)

Generated from `data/workouts.json` after backfilling from the Garmin "Export
Your Data" ZIP. 349 historical entries, `source: "garmin"`, spanning
**2012-04-20 to 2026-09-02**.

## Volume by type

| Type    | Count |
|---------|------:|
| HIIT    | 247   |
| RUNNING | 84    |
| OTHER   | 17    |
| CYCLING | 1     |

Per-year activity count: 2012–2014 had a device in use (52 activities total),
then a **gap from 2015–2021 with no recorded activity** (different device or
no tracking during that period), resuming in 2022 and ramping up sharply in
2024–2025 (56 and 160 activities respectively).

## HIIT (247 sessions)

- Avg duration: **58.2 min**
- Avg HR: **124.8 bpm** (max HR configured on the device: 192)
- Avg calories (Garmin estimate): **~1795 kcal**
- HR zone distribution across sessions with zone data (246/247):
  - Z0 (<50% max): 14.3%
  - Z1 (50–60%): 5.0%
  - Z2 (60–70%): 25.3%
  - Z3 (70–80%): 24.7%
  - Z4 (80–90%): 20.5%
  - Z5 (90%+): 10.1%

  Takeaway: despite being logged as "HIIT", the bulk of time sits in Z2–Z3
  (moderate, ~50% combined), with true near-max effort (Z5) only ~10% of
  total time — consistent with CrossFit-style sessions that mix steady
  conditioning work with shorter high-intensity intervals, rather than
  sustained max-effort throughout.

- Recent pace: **~2.4 HIIT sessions/week** in the last 90 days vs. **~1.9/week**
  in the 90 days before that — a modest upward trend.

## Running (84 sessions)

- Avg duration: **36.7 min**
- Avg HR: **155.4 bpm** — notably higher relative intensity than HIIT
  sessions, as expected for continuous running vs. mixed CrossFit work.

## Other (17 OTHER + 1 CYCLING)

Mostly manually-logged/legacy entries Garmin itself classified as
GENERIC/TRAINING rather than HIIT (e.g. "Strength", "Crossfit" with
non-HIIT sportType) — kept segmented as `OTHER` per the source's own
classification rather than reclassified by name.

## Data-quality note

A bug in the original `compute_hr_zones` zone-bucketing (inherited from the
pre-existing `garmin_sync.py`) crashed on any sample at or above 90% of max
HR, silently dropping ~92 of the most intense historical HIIT sessions on
the first backfill pass. Fixed in `garmin_sync.py` (see git history once the
repo exists) and the backfill re-run to recover them — the numbers above are
from the corrected run.
