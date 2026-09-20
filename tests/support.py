"""Shared test harness: runs analyze_workout.main() against temp files with a
fake Garmin, fake LLM, fake vision date-read and a recording Telegram."""

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _guard  # noqa: F401 -- installs the real-data guard (must come first)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analyze_workout as aw
import pending_store

LOCAL_TZ = pending_store.LOCAL_TZ


def days_ago(n):
    return (datetime.now(LOCAL_TZ) - timedelta(days=n)).strftime("%Y-%m-%d")


def workout(act_id, date_str, minutes=57.0, **extra):
    return {
        "activity_id": f"garmin_{act_id}", "source": "garmin", "name": "CrossFit no-gps", "type": "HIIT",
        "start_time": f"{date_str}T10:00:10", "duration_min": minutes, "avg_hr": 128.0, "max_hr": 165.0,
        "calories": 484.0, "training_effect": 3.0, "z1_min": 8.6, **extra,
    }


def entry(sent_at=None, workout_date=None, source=None, photo=None, texts=(), **extra):
    sent_at = sent_at or datetime.now(LOCAL_TZ)
    iso = sent_at.isoformat(timespec="seconds")
    return {
        "photo": photo, "texts": list(texts), "sent_at": iso, "last_at": iso,
        "workout_date": workout_date, "date_source": source if workout_date else None,
        "photo_date_checked": False, "asked_date": False, **extra,
    }


def patch_pending(test, tmp):
    """Point pending_store at temp files; returns the pending.json path."""
    path = tmp / "pending.json"
    (tmp / "boards").mkdir(exist_ok=True)
    for p in (
        mock.patch.object(pending_store, "PENDING_PATH", path),
        mock.patch.object(pending_store, "ARCHIVE_PATH", tmp / "archive.json"),
        mock.patch.object(pending_store, "ROOT", tmp),
        mock.patch.object(pending_store, "BOARDS_DIR", tmp / "boards"),
    ):
        p.start()
        test.addCleanup(p.stop)
    return path


def run_analyze(test, pending, workouts, llm_edit=None, vision=None, activities=(), lock_age_s=None):
    """pending: {id: entry}; vision(photo_rel) -> (date|None, cost, ok);
    llm_edit(workouts_list) edits the temp workouts.json like the real LLM would."""
    tmp = Path(tempfile.mkdtemp())
    wpath = tmp / "workouts.json"
    wpath.write_text(json.dumps({"workouts": workouts}), encoding="utf-8")
    pending_path = patch_pending(test, tmp)
    pending_path.write_text(json.dumps(pending), encoding="utf-8")
    calls = SimpleNamespace(llm=[], vision=[], telegram=[], garmin=0, garmin_args=[])
    lock = tmp / "analyze.lock"
    if lock_age_s is not None:  # simulate a run that is (or was) active
        lock.write_text("999", encoding="utf-8")
        os.utime(lock, (time.time() - lock_age_s,) * 2)

    def fake_llm(date_str, entry_, match, age, is_existing, budget_usd=None):
        calls.llm.append(SimpleNamespace(
            date=date_str, entry=entry_, match_id=match["activity_id"], is_existing=is_existing, budget=budget_usd))
        data = json.loads(wpath.read_text(encoding="utf-8"))
        if llm_edit:
            llm_edit(data["workouts"])
        elif is_existing:  # default: behave like a correct LLM
            next(w for w in data["workouts"] if w["activity_id"] == match["activity_id"])["insights"] = ["a"]
        else:
            data["workouts"].append({**match, "insights": ["a"]})
        wpath.write_text(json.dumps(data), encoding="utf-8")
        return subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"result": "insights", "total_cost_usd": 0.1}), stderr="")

    def fake_vision(photo, ref_day):
        calls.vision.append(photo)
        return vision(photo) if vision else (None, 0.0, True)

    def fake_garmin(*args, **kwargs):
        calls.garmin += 1
        calls.garmin_args.append((args, kwargs))
        return list(activities), None

    for p in (
        mock.patch.object(aw, "WORKOUTS_PATH", wpath),
        mock.patch.object(aw, "LAST_RUN_PATH", tmp / "last_run.json"),
        mock.patch.object(aw, "PAUSE_FLAG", tmp / "no.flag"),
        mock.patch.object(aw, "LOCK_PATH", lock),
        mock.patch.object(aw, "ROOT", tmp),
        mock.patch.object(aw, "load_env", return_value={}),
        mock.patch.object(aw, "find_git_exe", return_value="git"),
        mock.patch.object(aw, "send_telegram", side_effect=lambda t, c, text: calls.telegram.append(text)),
        mock.patch.object(aw, "run_llm_merge", side_effect=fake_llm),
        mock.patch.object(aw, "read_photo_date", side_effect=fake_vision),
        mock.patch.object(aw.garmin_health, "fetch_recent_safe", side_effect=fake_garmin),
        mock.patch.object(aw.subprocess, "run", return_value=mock.Mock(returncode=0)),  # update_log + git
    ):
        p.start()
        test.addCleanup(p.stop)
    aw.main()

    def read(path):
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    return SimpleNamespace(
        calls=calls, workouts=read(wpath)["workouts"], pending=read(pending_path),
        last_run=read(tmp / "last_run.json"), archive=read(tmp / "archive.json"), lock_exists=lock.exists(),
    )
