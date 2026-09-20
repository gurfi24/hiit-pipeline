#!/usr/bin/env python3
"""
Syncs Garmin Connect activities into a local workouts.json file. A direct
run is ALWAYS bounded to the last 7 days, regardless of last_synced -- pass
--full-history for an unbounded walk of your entire Garmin history (slow,
hits the API hard), or use --from-export-zip for a one-time bulk backfill
from a downloaded "Export Your Data" zip instead (no live login at all).

Every login attempt goes through garmin_health.py: it checks for an active
rate-limit cooldown first (refuses to even try if one is set), classifies
any failure, and sends a throttled Telegram alert -- so this script never
prints a raw traceback for an ordinary Garmin-side failure.

Setup:
    pip install garminconnect garth fitparse python-dotenv

    Set GARMIN_EMAIL and GARMIN_PASSWORD in .env (only needed for the very
    first login -- after that, garth caches the session to ~/.garminconnect
    and reuses/refreshes it automatically). If your account has MFA enabled,
    run scripts\\garmin_login.py once by hand from an interactive terminal
    first so you can enter the MFA code; background runs (e.g. from
    analyze_workout.py) will then reuse the cached session without needing
    MFA again until it expires.

Usage:
    python3 garmin_sync.py --data-dir ./data [--dry-run] [--full-history]
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

import env_setup

env_setup.load()  # populate os.environ from .env before anything reads GARMIN_EMAIL/PASSWORD

MAX_HR_ZONE_PCTS = [0.5, 0.6, 0.7, 0.8, 0.9, 1.01]
DEFAULT_MAX_HR = 190
MAX_SINCE_DAYS = 30  # hard cap on --since
LOCAL_TZ = ZoneInfo("Asia/Jerusalem")

# Garmin's own sportType/activityType strings -> our canonical `type` field.
# Everything not listed here (and not INVALID, which is dropped) becomes OTHER.
TYPE_MAP = {
    "hiit": "HIIT",
    "running": "RUNNING",
    "cycling": "CYCLING",
}


class GarminMFARequired(Exception):
    """Garmin needs an MFA code and no interactive terminal is attached.
    Not retryable -- only a human running scripts/garmin_login.py can fix it."""


class GarminNotConfigured(Exception):
    """No cached session and no GARMIN_EMAIL/GARMIN_PASSWORD to log in with."""


class GarminLoginIncomplete(Exception):
    """login() returned without raising, but the client still isn't actually
    authenticated. Must never happen silently -- always treat as a login
    failure, never call any other API method on this client."""


def _unwrap(exc):
    """garminconnect's own login() catches nearly everything and re-raises it
    wrapped as GarminConnectConnectionError(f"Login failed: {exc}") from exc --
    which preserves the REAL exception as __cause__ even though the outer
    type/message get genericized. Prefer the real cause so our own failure
    classification (garmin_health.classify_error) sees the true error type
    instead of a generic "connection" error for e.g. an actual 429 or MFA
    requirement."""
    return exc.__cause__ if exc.__cause__ is not None else exc


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
        if not client.client.is_authenticated:
            raise GarminLoginIncomplete("Cached-session login() returned without error, but the client is not authenticated.")
        return client
    except (FileNotFoundError, GarminConnectAuthenticationError):
        pass

    if not email or not password:
        raise GarminNotConfigured("No cached Garmin session (~/.garminconnect) and GARMIN_EMAIL/GARMIN_PASSWORD are not set.")

    def _prompt_mfa():
        # Called by garminconnect ONLY when Garmin actually asks for an MFA
        # code. Only prompt interactively if a real terminal is attached;
        # otherwise raise so the caller can report it (Telegram alert / CLI
        # error) rather than hang on input() in a background process. This
        # propagates up wrapped by garminconnect's own login() -- see _unwrap().
        if not sys.stdin.isatty():
            raise GarminMFARequired(
                "Garmin requires an MFA code. Run scripts\\garmin_login.py in an "
                "interactive terminal to re-authenticate; the session then gets "
                "cached to ~/.garminconnect and background runs won't need MFA "
                "again until it expires."
            )
        return input("Garmin MFA code: ").strip()

    # No return_on_mfa=True here: that mode makes garminconnect's login()
    # return early WITHOUT ever loading the profile (display_name etc.), so
    # every later API call fails with "Display name is not set" even on an
    # otherwise-successful login. Using prompt_mfa instead keeps login() on
    # its normal path, which always finishes loading the profile on success.
    client = Garmin(email=email, password=password, prompt_mfa=_prompt_mfa)
    try:
        client.login()
    except Exception as e:
        raise _unwrap(e) from e

    if not client.client.is_authenticated:
        raise GarminLoginIncomplete("login() returned without error, but the client is not authenticated.")

    token_store.mkdir(exist_ok=True)
    client.client.dump(str(token_store))  # client.client is the internal garth Client -- not client.garth
    return client


def _extract_fit_bytes(payload):
    """Garmin's ORIGINAL download is a ZIP wrapping the .fit for activities
    recorded natively on the watch, but a bare .fit for some uploaded ones.
    Sniff the magic bytes and return raw FIT bytes either way."""
    payload = bytes(payload)
    if payload[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            fit_names = [n for n in zf.namelist() if n.lower().endswith(".fit")]
            if not fit_names:
                raise ValueError(f"ZIP contains no .fit file (members: {zf.namelist()})")
            return zf.read(fit_names[0])
    if payload[8:12] == b".FIT":
        return payload
    raise ValueError(f"payload is neither ZIP nor FIT (first bytes: {payload[:16]!r})")


def _zones_from_max(max_hr, source):
    """Zone floors (bpm, Z1..Z5) as fixed % of a max HR: 50/60/70/80/90%."""
    return {
        "max_hr": int(max_hr),
        "floors": [round(max_hr * p) for p in MAX_HR_ZONE_PCTS[:-1]],
        "source": source,
    }


def resolve_hr_zones(client=None, workouts=None, env=None):
    """Decide the HR zone thresholds. Returns {"max_hr", "floors" (Z1..Z5 bpm),
    "source"}. Priority:
      1. Garmin's own zone settings (biometric-service heartRateZones)
      2. observed max HR across stored workouts
      3. age formula (220 - age), age computed at runtime from BIRTH_DATE
         (only the number is used; the birth date is never printed)
      4. DEFAULT_MAX_HR constant
    NOTE: get_user_summary()["maxHeartRate"] is that single DAY's max, not the
    profile max (it is None on a day with no data) -- never use it here."""
    if client is not None:
        try:
            for z in client.connectapi("/biometric-service/heartRateZones") or []:
                floors = [z.get(f"zone{i}Floor") for i in range(1, 6)]
                if z.get("maxHeartRateUsed") and all(floors):
                    if z.get("sport") in (None, "DEFAULT"):
                        return {"max_hr": int(z["maxHeartRateUsed"]), "floors": floors, "source": "garmin zone settings"}
        except Exception as e:
            print(f"  (could not read Garmin HR zone settings: {type(e).__name__})")

    if workouts is None:
        try:
            workouts = load_existing(Path(__file__).parent / "data")["workouts"]
        except Exception:
            workouts = []
    observed = max((w.get("max_hr") or 0 for w in workouts), default=0)
    if observed >= 120:  # ignore implausibly low history (e.g. only rest-day noise)
        return _zones_from_max(observed, "observed max HR in workouts.json")

    age = env_setup.compute_age(env)
    if age is not None:
        return _zones_from_max(220 - age, "age formula (220 - age)")
    return _zones_from_max(DEFAULT_MAX_HR, "default constant")


def compute_hr_zones(fit_bytes, max_hr, floors=None):
    """Parse a FIT file's record messages and return minutes spent per HR zone.
    `floors` = Z1..Z5 lower bounds in bpm; defaults to 50/60/70/80/90% of max_hr."""
    from fitparse import FitFile

    if floors is None:
        floors = _zones_from_max(max_hr, "")["floors"]
    fitfile = FitFile(io.BytesIO(_extract_fit_bytes(fit_bytes)))
    records = []
    for msg in fitfile.get_messages("record"):
        d = msg.get_values()
        if d.get("heart_rate") is not None and d.get("timestamp") is not None:
            records.append((d["timestamp"], d["heart_rate"]))
    return minutes_per_zone(records, floors)


def minutes_per_zone(records, floors):
    """records: [(timestamp, hr)]. Each interval is credited to the zone of its
    starting sample; z0 = below the Z1 floor. Returns {"z0_min": ..., "z5_min": ...}."""
    records = sorted(records)
    zone_time = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for i in range(1, len(records)):
        t_prev, hr_prev = records[i - 1]
        t_cur, _ = records[i]
        zone = sum(1 for f in floors if hr_prev >= f)
        zone_time[zone] += (t_cur - t_prev).total_seconds()
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


def _build_workout_entry(act, client, hr_zones):
    """Build one normalized workout dict from a live get_activities() summary,
    downloading its FIT file for HR-zone minutes. Shared by the full/incremental
    sync in main() and the on-demand fetch_recent() used by analyze_workout.py."""
    act_id = act["activityId"]
    try:
        fit_bytes = client.download_activity(act_id, dl_fmt=client.ActivityDownloadFormat.ORIGINAL)
        zones = compute_hr_zones(fit_bytes, hr_zones["max_hr"], hr_zones["floors"])
    except Exception as e:
        print(f"  skipped FIT parse for {act_id}: {e}")
        zones = {}
    wtype = _classify_type(act.get("activityType", {}).get("typeKey"), None)
    return {
        # "garmin_" prefix matches the export-zip backfill's convention --
        # without it, dedup against the existing DB silently breaks.
        "activity_id": f"garmin_{act_id}",
        "source": "garmin",
        "name": act.get("activityName"),
        "type": wtype or "OTHER",
        # DB convention is ISO "T" (all stored entries); the API returns a space.
        "start_time": (act.get("startTimeLocal") or "").replace(" ", "T") or None,
        "duration_min": round((act.get("duration") or 0) / 60, 1),
        "avg_hr": act.get("averageHR"),
        "max_hr": act.get("maxHR"),
        "calories": act.get("calories"),
        "training_effect": act.get("aerobicTrainingEffect"),
        "anaerobic_training_effect": act.get("anaerobicTrainingEffect"),
        **zones,
    }


def iter_recent_summaries(client, cutoff):
    """Yield raw get_activities() summaries newer than `cutoff` (an aware
    datetime), most-recent-first. No FIT downloads -- listing only."""
    start = 0
    batch_size = 20
    while True:
        batch = client.get_activities(start, batch_size)
        if not batch:
            return
        for act in batch:
            start_local = act.get("startTimeLocal")
            if not start_local:
                continue
            if datetime.fromisoformat(start_local).replace(tzinfo=LOCAL_TZ) < cutoff:
                return
            yield act
        start += batch_size
        if start > 200:  # safety cap -- a <=30-day window should never need this many
            return


def fetch_recent(client, hr_zones, days=7, cutoff=None, known_ids=None):
    """Pull activities from the last `days` days (or since `cutoff`) via the
    live API. Used by analyze_workout.py on /start -- deliberately NOT a
    full/incremental sync, just a short lookback so a fresh watch sync is
    picked up quickly. `known_ids` (garmin_<id> strings) are skipped before
    the FIT download, so already-stored activities cost no extra API calls."""
    from datetime import timedelta

    cutoff = cutoff or datetime.now(LOCAL_TZ) - timedelta(days=days)
    results = []
    for act in iter_recent_summaries(client, cutoff):
        if known_ids and f"garmin_{act['activityId']}" in known_ids:
            continue
        results.append(_build_workout_entry(act, client, hr_zones))
    return results


def load_existing(data_dir: Path):
    path = data_dir / "workouts.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"last_synced": None, "workouts": []}


def save(data_dir: Path, data):
    path = data_dir / "workouts.json"
    # utf-8 explicitly -- Windows' default write_text() encoding is the
    # locale codepage (cp1252 here), which crashes on Hebrew text.
    path.write_text(json.dumps(data, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(data['workouts'])} workouts to {path}")


def _full_history_walk(client, hr_zones, known_ids):
    """Escape hatch for --full-history: walks Garmin's ENTIRE activity list
    (most-recent-first), skipping already-known ids, with no time bound.
    Slow and hits the API hard -- only use deliberately. Prefer the default
    7-day-bounded sync, or --from-export-zip for a one-time bulk backfill."""
    batch_size = 20
    start = 0
    new_workouts = []
    while True:
        batch = client.get_activities(start, batch_size)
        if not batch:
            break
        for act in batch:
            act_id = f"garmin_{act['activityId']}"  # match _build_workout_entry's stored id format
            if act_id in known_ids:
                continue
            print(f"Fetching {act.get('activityName')} ({act.get('startTimeLocal')})...")
            new_workouts.append(_build_workout_entry(act, client, hr_zones))
        start += batch_size
        if start > 2000:  # safety cap
            break
    return new_workouts


def main():
    import garmin_health  # lazy: garmin_health imports FROM this module, so a top-level import here would be circular

    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="./data")
    parser.add_argument(
        "--full-history",
        action="store_true",
        help="Force an unbounded walk of Garmin's entire activity history via the live API "
        "(slow, hits the API hard). Without this flag, a direct run is ALWAYS bounded to "
        "the last 7 days, regardless of last_synced.",
    )
    parser.add_argument(
        "--from-export-zip",
        metavar="ZIP_PATH",
        help="One-time backfill from a local Garmin 'Export Your Data' zip "
        "instead of the live API. No Garmin login required.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print what would be added, but never write to workouts.json.",
    )
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help=f"Look back to this local date instead of 7 days (max {MAX_SINCE_DAYS} days back). "
        "Works with or without --dry-run; combine with --dry-run to preview first.",
    )
    args = parser.parse_args()

    since_cutoff = None
    if args.since:
        if args.full_history:
            sys.exit("--since and --full-history are mutually exclusive.")
        try:
            since_date = datetime.strptime(args.since, "%Y-%m-%d").date()
        except ValueError:
            sys.exit("--since must be YYYY-MM-DD.")
        age_days = (datetime.now(LOCAL_TZ).date() - since_date).days
        if age_days < 0 or age_days > MAX_SINCE_DAYS:
            sys.exit(f"--since must be within the last {MAX_SINCE_DAYS} days (got {age_days} days ago).")
        since_cutoff = datetime.combine(since_date, datetime.min.time(), tzinfo=LOCAL_TZ)

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    if args.from_export_zip:
        sync_from_export_zip(args.from_export_zip, data_dir)
        return

    store = load_existing(data_dir)
    env = {k: os.environ.get(k) for k in ("GARMIN_EMAIL", "GARMIN_PASSWORD", "GITHUB_TOKEN", "TELEGRAM_BOT_TOKEN")}
    telegram_token = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = None
    state_path = data_dir / "telegram_state.json"
    if state_path.exists():
        chat_id = json.loads(state_path.read_text(encoding="utf-8")).get("chat_id")

    blocked, msg = garmin_health.cooldown_status()
    if blocked:
        sys.exit(msg)

    client, err = garmin_health.login_only_safe(telegram_token, chat_id, env)
    if err:
        kind, reason = err
        sys.exit(f"Garmin login failed ({kind}): {reason}")

    hr_zones = resolve_hr_zones(client, store["workouts"], os.environ)
    print(
        f"HR zones from: {hr_zones['source']} | max HR {hr_zones['max_hr']} | "
        f"Z1-Z5 floors (bpm): {'/'.join(str(f) for f in hr_zones['floors'])}"
    )
    known_ids = {w["activity_id"] for w in store["workouts"]}

    if args.full_history:
        new_workouts = _full_history_walk(client, hr_zones, known_ids)
    else:
        # Default: ALWAYS bounded to 7 days, regardless of last_synced -- a
        # direct run of this script must never accidentally re-walk a large
        # chunk of history just because last_synced is old or missing.
        # --since (explicit, <= MAX_SINCE_DAYS) widens that window.
        if since_cutoff:
            print(f"Garmin activities since {args.since} (raw listing, no downloads):")
            for act in iter_recent_summaries(client, since_cutoff):
                flag = "in DB" if f"garmin_{act['activityId']}" in known_ids else "NOT in DB"
                print(
                    f"  {act['startTimeLocal']} | garmin_{act['activityId']} | {act.get('activityName')} | "
                    f"{act.get('activityType', {}).get('typeKey')} | {round((act.get('duration') or 0) / 60, 1)} min | {flag}"
                )
        recent = fetch_recent(client, hr_zones, days=7, cutoff=since_cutoff, known_ids=known_ids)
        new_workouts = [w for w in recent if w["activity_id"] not in known_ids]

    label = "[dry-run] Would add" if args.dry_run else "Adding"
    print(f"{label} {len(new_workouts)} new workout(s):")
    for w in new_workouts:
        print(f"  {w['start_time']} | {w['type']} | {w.get('name')} | {w.get('duration_min')} min")
        zones = [w.get(f"z{z}_min") for z in range(1, 6)]
        zone_str = "/".join("-" if v is None else str(v) for v in zones)
        print(
            f"    id={w['activity_id']}  zones Z1-Z5 min: {zone_str}"
            f"  avg_hr={w.get('avg_hr')}  max_hr={w.get('max_hr')}  calories={w.get('calories')}"
            f"  TE={w.get('training_effect')}  anaerobic_TE={w.get('anaerobic_training_effect')}"
        )
    if args.dry_run:
        print("[dry-run] No changes written to workouts.json.")
        return
    if not new_workouts:
        print("Nothing new -- workouts.json left untouched.")
        return

    store["workouts"] = new_workouts + store["workouts"]
    store["workouts"].sort(key=lambda w: w["start_time"])
    # Existing convention: last_synced is the newest stored workout's start_time.
    store["last_synced"] = max(w["start_time"] for w in new_workouts)
    save(data_dir, store)


if __name__ == "__main__":
    main()
