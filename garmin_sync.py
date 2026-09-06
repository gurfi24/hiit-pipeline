#!/usr/bin/env python3
"""
Syncs Garmin Connect activities into a local workouts.json file.
First run: pulls full history. Subsequent runs: only new activities since last sync.

Setup:
    pip install garminconnect garth fitparse

    export GARMIN_EMAIL="you@example.com"
    export GARMIN_PASSWORD="your-password"

Usage:
    python3 garmin_sync.py --data-dir ./data
"""

import argparse
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

MAX_HR_ZONE_PCTS = [0.5, 0.6, 0.7, 0.8, 0.9, 1.01]
DEFAULT_MAX_HR = 190
LOCAL_TZ = ZoneInfo("Asia/Jerusalem")

# Garmin's own sportType/activityType strings -> our canonical `type` field.
# Everything not listed here (and not INVALID, which is dropped) becomes OTHER.
TYPE_MAP = {
    "hiit": "HIIT",
    "running": "RUNNING",
    "cycling": "CYCLING",
}


def get_client():
    # Imported lazily: the export-zip backfill path (--from-export-zip) needs
    # neither garminconnect nor a live login, so don't require it at import time.
    from garminconnect import Garmin, GarminConnectAuthenticationError

    token_store = Path.home() / ".garminconnect"
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")

    client = Garmin()
    try:
        client.login(str(token_store))
        return client
    except (FileNotFoundError, GarminConnectAuthenticationError):
        pass

    if not email or not password:
        sys.exit("Set GARMIN_EMAIL and GARMIN_PASSWORD environment variables.")

    client = Garmin(email=email, password=password)
    client.login()
    token_store.mkdir(exist_ok=True)
    client.garth.dump(str(token_store))
    return client


def compute_hr_zones(fit_bytes, max_hr):
    """Parse a FIT file's record messages and return minutes spent per HR zone."""
    import io
    from fitparse import FitFile

    fitfile = FitFile(io.BytesIO(fit_bytes))
    records = []
    for msg in fitfile.get_messages("record"):
        d = msg.get_values()
        if d.get("heart_rate") is not None and d.get("timestamp") is not None:
            records.append((d["timestamp"], d["heart_rate"]))
    records.sort()

    zone_time = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    zone_lower_bounds = MAX_HR_ZONE_PCTS[:-1]  # 0.5, 0.6, 0.7, 0.8, 0.9 -> zones 1-5
    for i in range(1, len(records)):
        t_prev, hr_prev = records[i - 1]
        t_cur, _ = records[i]
        dt = (t_cur - t_prev).total_seconds()
        pct = hr_prev / max_hr
        zone = 0
        for z, lo in enumerate(zone_lower_bounds, start=1):
            if pct >= lo:
                zone = z
        zone_time[zone] += dt

    return {f"z{z}_min": round(t / 60, 1) for z, t in zone_time.items()}


def _classify_type(sport_type, activity_type):
    """Map Garmin's own sportType/activityType strings to our canonical `type`
    field. Returns None for INVALID entries, which should be dropped."""
    raw = (sport_type or activity_type or "OTHER")
    raw = str(raw).strip().lower()
    if raw == "invalid":
        return None
    return TYPE_MAP.get(raw, "OTHER")


def _load_export_metadata(outer: zipfile.ZipFile):
    """Read max HR + all historical activity summaries out of a Garmin
    "Export Your Data" zip. Returns (max_hr, activities) where activities is
    a list of normalized dicts (one per DI-Connect-Fitness summarized
    activity, oldest and newest export files combined)."""
    max_hr = DEFAULT_MAX_HR
    for name in outer.namelist():
        if name.endswith("_heartRateZones.json"):
            try:
                zones = json.loads(outer.read(name))
                if zones and zones[0].get("maxHeartRateUsed"):
                    max_hr = zones[0]["maxHeartRateUsed"]
            except Exception:
                pass
            break

    activities = []
    for name in outer.namelist():
        if "DI-Connect-Fitness" in name and name.endswith("_summarizedActivities.json"):
            obj = json.loads(outer.read(name))
            if isinstance(obj, list):
                obj = obj[0] if obj else {}
            for a in obj.get("summarizedActivitiesExport", []):
                activities.append(a)

    return max_hr, activities


def _find_upload_zip_names(outer: zipfile.ZipFile):
    return [
        n
        for n in outer.namelist()
        if "DI-Connect-Uploaded-Files" in n and n.lower().endswith(".zip")
    ]


def _match_metadata(start_utc: datetime, activities, used, tolerance_s=300):
    """Find the closest un-used activity summary within `tolerance_s` of a
    FIT session's UTC start time. Marks it used and returns it, or None."""
    best_idx, best_diff = None, None
    target = start_utc.timestamp()
    for i, a in enumerate(activities):
        if i in used:
            continue
        begin = a.get("beginTimestamp")
        if begin is None:
            continue
        diff = abs(begin / 1000.0 - target)
        if diff <= tolerance_s and (best_diff is None or diff < best_diff):
            best_idx, best_diff = i, diff
    if best_idx is not None:
        used.add(best_idx)
        return activities[best_idx]
    return None


def _local_time_str(meta, fallback_utc: datetime):
    if meta and meta.get("startTimeLocal") is not None:
        return datetime.utcfromtimestamp(meta["startTimeLocal"] / 1000.0).isoformat()
    return fallback_utc.astimezone(LOCAL_TZ).replace(tzinfo=None).isoformat()


def sync_from_export_zip(zip_path: str, data_dir: Path):
    """One-time backfill: parse a local Garmin "Export Your Data" zip
    (JSON summaries + a nested zip of raw .fit uploads) into workouts.json,
    tagging every entry source: "garmin". No live Garmin login involved."""
    from fitparse import FitFile

    outer = zipfile.ZipFile(zip_path)
    max_hr, activities = _load_export_metadata(outer)
    print(f"Found {len(activities)} historical activity summaries (max_hr={max_hr}).")

    used = set()
    workouts = []

    upload_zip_names = _find_upload_zip_names(outer)
    fit_activity_count = 0
    for uz_name in upload_zip_names:
        inner = zipfile.ZipFile(io.BytesIO(outer.read(uz_name)))
        fit_names = inner.namelist()
        print(f"Scanning {len(fit_names)} FIT files in {uz_name}...")
        for fname in fit_names:
            data = inner.read(fname)
            try:
                ff = FitFile(io.BytesIO(data))
                file_id = next(ff.get_messages("file_id"), None)
                if not file_id or file_id.get_value("type") != "activity":
                    continue
            except Exception:
                continue

            try:
                ff = FitFile(io.BytesIO(data))
                sess = next(ff.get_messages("session"), None)
                if sess is None:
                    continue
                start_utc = sess.get_value("start_time")
                if start_utc.tzinfo is None:
                    start_utc = start_utc.replace(tzinfo=timezone.utc)

                meta = _match_metadata(start_utc, activities, used)
                sport_type = meta.get("sportType") if meta else None
                activity_type = meta.get("activityType") if meta else sess.get_value("sport")
                wtype = _classify_type(sport_type, activity_type)
                if wtype is None:
                    continue

                zones = compute_hr_zones(data, max_hr)
                fit_activity_count += 1

                name = (meta or {}).get("name") or f"Garmin {activity_type or 'activity'}"
                duration_min = (
                    round(meta["duration"] / 60000.0, 1)
                    if meta and meta.get("duration")
                    else round((sess.get_value("total_elapsed_time") or 0) / 60.0, 1)
                )
                activity_id = (
                    f"garmin_{meta['activityId']}" if meta else f"garmin_fit_{fname.rsplit('.', 1)[0]}"
                )

                workouts.append(
                    {
                        "activity_id": activity_id,
                        "source": "garmin",
                        "name": name,
                        "type": wtype,
                        "start_time": _local_time_str(meta, start_utc),
                        "duration_min": duration_min,
                        "avg_hr": (meta or {}).get("avgHr") or sess.get_value("avg_heart_rate"),
                        "max_hr": (meta or {}).get("maxHr") or sess.get_value("max_heart_rate"),
                        "calories": (meta or {}).get("calories") or sess.get_value("total_calories"),
                        "training_effect": (meta or {}).get("aerobicTrainingEffect")
                        or sess.get_value("total_training_effect"),
                        "anaerobic_training_effect": (meta or {}).get("anaerobicTrainingEffect")
                        or sess.get_value("total_anaerobic_training_effect"),
                        **zones,
                    }
                )
            except Exception as e:
                print(f"  skipped {fname} ({e})")

    print(f"Matched {fit_activity_count} FIT files to workout entries.")

    # Historical activities with no corresponding FIT file (older than the
    # raw-upload retention window, or manually entered) still get an entry,
    # just without HR-zone minutes.
    json_only = 0
    for i, a in enumerate(activities):
        if i in used:
            continue
        wtype = _classify_type(a.get("sportType"), a.get("activityType"))
        if wtype is None:
            continue
        begin = a.get("beginTimestamp")
        if begin is None:
            continue
        start_utc = datetime.fromtimestamp(begin / 1000.0, tz=timezone.utc)
        workouts.append(
            {
                "activity_id": f"garmin_{a['activityId']}",
                "source": "garmin",
                "name": a.get("name") or f"Garmin {a.get('activityType') or 'activity'}",
                "type": wtype,
                "start_time": _local_time_str(a, start_utc),
                "duration_min": round((a.get("duration") or 0) / 60000.0, 1),
                "avg_hr": a.get("avgHr"),
                "max_hr": a.get("maxHr"),
                "calories": a.get("calories"),
                "training_effect": a.get("aerobicTrainingEffect"),
                "anaerobic_training_effect": a.get("anaerobicTrainingEffect"),
            }
        )
        json_only += 1
    print(f"Added {json_only} JSON-only entries (no matching FIT file, no HR zones).")

    store = load_existing(data_dir)
    existing_ids = {w["activity_id"] for w in store["workouts"]}
    new_workouts = [w for w in workouts if w["activity_id"] not in existing_ids]
    skipped_dupes = len(workouts) - len(new_workouts)
    if skipped_dupes:
        print(f"Skipped {skipped_dupes} entries already present in workouts.json.")

    store["workouts"] = store["workouts"] + new_workouts
    store["workouts"].sort(key=lambda w: w["start_time"])
    save(data_dir, store)


def load_existing(data_dir: Path):
    path = data_dir / "workouts.json"
    if path.exists():
        return json.loads(path.read_text())
    return {"last_synced": None, "workouts": []}


def save(data_dir: Path, data):
    path = data_dir / "workouts.json"
    path.write_text(json.dumps(data, indent=2, default=str))
    print(f"Saved {len(data['workouts'])} workouts to {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="./data")
    parser.add_argument(
        "--full-history",
        action="store_true",
        help="Force pulling full history even if workouts.json already exists",
    )
    parser.add_argument(
        "--from-export-zip",
        metavar="ZIP_PATH",
        help="One-time backfill from a local Garmin 'Export Your Data' zip "
        "instead of the live API. No Garmin login required.",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    if args.from_export_zip:
        sync_from_export_zip(args.from_export_zip, data_dir)
        return

    store = load_existing(data_dir)
    is_first_run = store["last_synced"] is None or args.full_history

    client = get_client()
    max_hr = client.get_user_summary(datetime.now().strftime("%Y-%m-%d")).get(
        "maxHeartRate"
    ) or 190

    known_ids = {w["activity_id"] for w in store["workouts"]}

    # Pull activities in batches until we hit ones we already have (incremental)
    # or exhaust history (first run).
    batch_size = 20
    start = 0
    new_workouts = []
    while True:
        batch = client.get_activities(start, batch_size)
        if not batch:
            break
        stop = False
        for act in batch:
            act_id = act["activityId"]
            if act_id in known_ids and not is_first_run:
                stop = True
                break
            if act_id in known_ids:
                continue
            print(f"Fetching {act.get('activityName')} ({act.get('startTimeLocal')})...")
            try:
                fit_bytes = client.download_activity(
                    act_id, dl_fmt=client.ActivityDownloadFormat.ORIGINAL
                )
                zones = compute_hr_zones(fit_bytes, max_hr)
            except Exception as e:
                print(f"  skipped (couldn't parse FIT: {e})")
                zones = {}
            new_workouts.append(
                {
                    "activity_id": act_id,
                    "name": act.get("activityName"),
                    "type": act.get("activityType", {}).get("typeKey"),
                    "start_time": act.get("startTimeLocal"),
                    "duration_min": round((act.get("duration") or 0) / 60, 1),
                    "avg_hr": act.get("averageHR"),
                    "max_hr": act.get("maxHR"),
                    "calories": act.get("calories"),
                    "training_effect": act.get("trainingEffect"),
                    "anaerobic_training_effect": act.get("anaerobicTrainingEffect"),
                    **zones,
                }
            )
        if stop or not is_first_run and any(a["activityId"] in known_ids for a in batch):
            break
        start += batch_size
        if start > 2000:  # safety cap
            break

    store["workouts"] = new_workouts + store["workouts"]
    store["last_synced"] = datetime.now().isoformat()
    save(data_dir, store)


if __name__ == "__main__":
    main()
