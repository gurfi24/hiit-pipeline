#!/usr/bin/env python3
"""
Pulls new exercises from Polar AccessLink (transaction-based API) and merges
them into data/workouts.json, tagged source: "polar".

NOTE: as of writing this, the Polar account behind POLAR_ACCESS_TOKEN has
zero exercises synced yet, so the exact shape of a real exercise summary
(and whether HR-zone time is included directly, or needs to be computed
from /samples) has not been verified against live data -- see
polar_telegram_plan.md's "open questions" section. This is a best-effort
implementation of the documented v3 API; re-check field names against the
first real response once the user has done a Polar-tracked workout.

Usage:
    python polar_sync.py [--data-dir ./data]
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ENV_PATH = Path(__file__).parent / ".env"
LOCAL_TZ = ZoneInfo("Asia/Jerusalem")
DEFAULT_MAX_HR = 192  # matches the Garmin export's maxHeartRateUsed; override via MAX_HR in .env
BASE = "https://www.polaraccesslink.com/v3"
ZONE_LOWER_BOUNDS = [0.5, 0.6, 0.7, 0.8, 0.9]  # -> zones 1-5, same convention as garmin_sync.py

SPORT_TYPE_MAP = {
    "CROSSFIT": "HIIT",
    "OTHER_INDOOR": "HIIT",
    "STRENGTH_TRAINING": "HIIT",
    "RUNNING": "RUNNING",
    "TRAIL_RUNNING": "RUNNING",
    "CYCLING": "CYCLING",
    "MOUNTAIN_BIKING": "CYCLING",
}

ISO8601_DURATION_RE = re.compile(
    r"P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>[\d.]+)S)?"
)


def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def parse_iso8601_duration_minutes(s):
    if not s:
        return None
    m = ISO8601_DURATION_RE.match(s)
    if not m:
        return None
    parts = {k: float(v) if v else 0 for k, v in m.groupdict().items()}
    total_seconds = parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]
    return round(total_seconds / 60, 1)


def classify_type(sport):
    if not sport:
        return "OTHER"
    return SPORT_TYPE_MAP.get(str(sport).upper(), "OTHER")


def compute_zones_from_samples(samples, max_hr):
    """Fallback if the exercise summary doesn't include zone time directly:
    compute it from a raw HR sample series, same bucketing as garmin_sync.py."""
    points = []
    for s in samples:
        hr = s.get("value")
        t = s.get("date-time") or s.get("recording-date-time")
        if hr is not None and t is not None:
            points.append((t, hr))
    points.sort()
    zone_time = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for i in range(1, len(points)):
        t_prev, hr_prev = points[i - 1]
        t_cur, _ = points[i]
        try:
            dt = (datetime.fromisoformat(t_cur) - datetime.fromisoformat(t_prev)).total_seconds()
        except ValueError:
            dt = 1  # samples are usually 1s apart; fall back rather than crash
        pct = hr_prev / max_hr
        zone = 0
        for z, lo in enumerate(ZONE_LOWER_BOUNDS, start=1):
            if pct >= lo:
                zone = z
        zone_time[zone] += dt
    return {f"z{z}_min": round(t / 60, 1) for z, t in zone_time.items()}


def load_existing(data_dir: Path):
    path = data_dir / "workouts.json"
    if path.exists():
        return json.loads(path.read_text())
    return {"last_synced": None, "workouts": []}


def save(data_dir: Path, data):
    path = data_dir / "workouts.json"
    path.write_text(json.dumps(data, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="./data")
    args = parser.parse_args()

    env = load_env()
    token = env.get("POLAR_ACCESS_TOKEN")
    user_id = env.get("POLAR_USER_ID")
    max_hr = float(env.get("MAX_HR", DEFAULT_MAX_HR))
    if not token or not user_id:
        sys.exit("POLAR_ACCESS_TOKEN / POLAR_USER_ID missing from .env -- run polar_oauth_setup.py first")

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    store = load_existing(data_dir)
    known_ids = {w["activity_id"] for w in store["workouts"]}

    tx_url = f"{BASE}/users/{user_id}/exercise-transactions"
    r = requests.post(tx_url, headers=headers)
    if r.status_code == 204:
        print("No new exercise data since last check.")
        print("RESULT_JSON:" + json.dumps({"new_activity_ids": []}))
        return
    r.raise_for_status()
    tx_id = r.json()["transaction-id"]

    r = requests.get(f"{tx_url}/{tx_id}/exercises", headers=headers)
    r.raise_for_status()
    exercise_refs = r.json().get("exercises", [])
    print(f"{len(exercise_refs)} new exercise(s) in this transaction.")

    new_workouts = []
    for ref in exercise_refs:
        # `ref` is a full resource URI per the documented API.
        r = requests.get(ref, headers=headers)
        r.raise_for_status()
        ex = r.json()

        exercise_id = ex.get("id") or ref.rsplit("/", 1)[-1]
        if f"polar_{exercise_id}" in known_ids:
            continue

        start_time = ex.get("start-time")  # local time per Polar's docs, no "Z"
        duration_min = parse_iso8601_duration_minutes(ex.get("duration"))
        hr = ex.get("heart-rate") or {}

        zones = {}
        # Best-effort: look for zone time directly on the summary first.
        if "heart-rate-zones" in ex:
            for i, z in enumerate(ex["heart-rate-zones"], start=1):
                mins = parse_iso8601_duration_minutes(z.get("in-zone"))
                if mins is not None:
                    zones[f"z{i}_min"] = mins
        else:
            samples_r = requests.get(f"{ref}/samples", headers=headers)
            if samples_r.status_code == 200:
                samples = samples_r.json().get("samples", [])
                if samples:
                    zones = compute_zones_from_samples(samples, max_hr)

        new_workouts.append(
            {
                "activity_id": f"polar_{exercise_id}",
                "source": "polar",
                "name": ex.get("detailed-sport-info") or ex.get("sport") or "Polar exercise",
                "type": classify_type(ex.get("sport")),
                "start_time": start_time,
                "duration_min": duration_min,
                "avg_hr": hr.get("average"),
                "max_hr": hr.get("maximum"),
                "calories": ex.get("calories"),
                "training_effect": ex.get("training-load"),
                **zones,
            }
        )

    if new_workouts:
        store["workouts"].extend(new_workouts)
        store["workouts"].sort(key=lambda w: w["start_time"])

    store["last_synced"] = datetime.now().isoformat()
    save(data_dir, store)

    # Commit the transaction only after successfully writing to workouts.json,
    # per the plan -- otherwise the same data is safely re-returned next time.
    requests.put(f"{tx_url}/{tx_id}", headers=headers)

    print(f"Added {len(new_workouts)} new Polar workout(s).")
    # Machine-parseable summary line for run_pipeline.py -- lets it scope the
    # recommendations-writing Claude call to exactly these new entries.
    print("RESULT_JSON:" + json.dumps({"new_activity_ids": [w["activity_id"] for w in new_workouts]}))


if __name__ == "__main__":
    main()
