"""/start on a date that already has a garmin_ entry must MERGE the photo,
results text and insights into that entry -- never duplicate it, never skip it.
No network, no LLM: the LLM step is replaced by a fake that edits a temp
workouts.json the way the prompt tells the real one to."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analyze_workout as aw
import pending_store

DAY = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def workout(act_id, date_str=DAY, minutes=57.0, **extra):
    return {
        "activity_id": f"garmin_{act_id}", "source": "garmin", "name": "CrossFit no-gps", "type": "HIIT",
        "start_time": f"{date_str} 10:00:10", "duration_min": minutes, "avg_hr": 128.0, "max_hr": 165.0,
        "calories": 484.0, "training_effect": 3.0, "z1_min": 8.6, **extra,
    }


class MatchTests(unittest.TestCase):
    def test_existing_entry_on_date_is_matched_for_merge(self):
        match, is_existing = aw.match_garmin_workout(DAY, [], [workout(1)])
        self.assertEqual(match["activity_id"], "garmin_1")
        self.assertTrue(is_existing)

    def test_same_activity_in_both_lists_is_not_duplicated(self):
        match, is_existing = aw.match_garmin_workout(DAY, [workout(1)], [workout(1)])
        self.assertEqual(match["activity_id"], "garmin_1")
        self.assertTrue(is_existing)

    def test_longest_activity_wins_across_existing_and_new(self):
        new = [workout(2, minutes=65), workout(3, minutes=10)]
        match, is_existing = aw.match_garmin_workout(DAY, new, [workout(1, minutes=30)])
        self.assertEqual(match["activity_id"], "garmin_2")
        self.assertFalse(is_existing)
        # ...and the other way round: a longer existing entry beats a shorter new one
        match, is_existing = aw.match_garmin_workout(DAY, [workout(3, minutes=10)], [workout(1, minutes=70)])
        self.assertEqual(match["activity_id"], "garmin_1")
        self.assertTrue(is_existing)

    def test_tie_prefers_existing(self):
        match, is_existing = aw.match_garmin_workout(DAY, [workout(2, minutes=57)], [workout(1, minutes=57)])
        self.assertEqual(match["activity_id"], "garmin_1")
        self.assertTrue(is_existing)

    def test_other_dates_ignored_and_no_match_is_none(self):
        other = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        self.assertEqual(aw.match_garmin_workout(DAY, [workout(2, other)], [workout(1, other)]), (None, False))


class ValidateMergeTests(unittest.TestCase):
    def setUp(self):
        self.before = [workout(9, "2026-09-01"), workout(1)]

    def merged(self):
        after = copy.deepcopy(self.before)
        after[1].update(image="boards/x.jpg", notes="squat 80kg", insights=["a", "b", "c"])
        return after

    def test_correct_merge_passes(self):
        self.assertEqual(aw.validate_merge(self.before, self.merged(), "garmin_1", True), [])

    def test_duplicate_entry_is_rejected(self):
        after = self.merged()
        after.append(copy.deepcopy(after[1]))
        self.assertTrue(aw.validate_merge(self.before, after, "garmin_1", True))

    def test_skipped_entry_is_rejected(self):
        self.assertTrue(aw.validate_merge(self.before, [self.before[0]], "garmin_1", True))

    def test_changed_garmin_field_is_rejected(self):
        after = self.merged()
        after[1]["avg_hr"] = 999
        self.assertTrue(any("avg_hr" in p for p in aw.validate_merge(self.before, after, "garmin_1", True)))

    def test_unrelated_entry_touched_is_rejected(self):
        after = self.merged()
        after[0]["notes"] = "oops"
        self.assertTrue(aw.validate_merge(self.before, after, "garmin_1", True))

    def test_add_mode_requires_exactly_one_new_entry(self):
        new = workout(2, "2026-09-02")
        self.assertEqual(aw.validate_merge(self.before, self.before + [new], "garmin_2", False), [])
        self.assertTrue(aw.validate_merge(self.before, self.before, "garmin_2", False))  # nothing added
        self.assertTrue(aw.validate_merge(self.before, self.before + [new, new], "garmin_2", False))  # duplicated


class PromptTests(unittest.TestCase):
    def prompt_for(self, is_existing):
        with mock.patch.object(aw.subprocess, "run") as run:
            aw.run_llm_merge(DAY, {"photo": "boards/x.jpg", "texts": ["squat 80kg"]}, workout(1), None, is_existing)
        return run.call_args[0][0][2]

    def test_merge_mode_prompt_updates_in_place(self):
        p = self.prompt_for(True)
        self.assertIn("ALREADY has an entry", p)
        self.assertIn("do NOT add a new entry", p)
        self.assertNotIn("Add ONE new entry", p)

    def test_add_mode_prompt_unchanged(self):
        self.assertIn("Add ONE new entry", self.prompt_for(False))


class MainMergeFlowTests(unittest.TestCase):
    """Drive main() end to end with a fake LLM against temp files."""

    def run_main(self, llm_edit):
        tmp = Path(tempfile.mkdtemp())
        wpath = tmp / "workouts.json"
        wpath.write_text(json.dumps({"workouts": [workout(9, "2026-09-01"), workout(1)]}), encoding="utf-8")
        pending_path = tmp / "pending.json"
        pending_path.write_text(
            json.dumps({DAY: {"photo": "boards/x.jpg", "texts": ["squat 80kg"], "received_at": "x"}}), encoding="utf-8"
        )
        seen = {}

        def fake_llm(date_str, entry, match, age, is_existing):
            seen["is_existing"], seen["match_id"] = is_existing, match["activity_id"]
            data = json.loads(wpath.read_text(encoding="utf-8"))
            llm_edit(data["workouts"])
            wpath.write_text(json.dumps(data), encoding="utf-8")
            return subprocess.CompletedProcess(
                [], 0, stdout=json.dumps({"result": "insights", "total_cost_usd": 0.1}), stderr=""
            )

        patches = [
            mock.patch.object(aw, "WORKOUTS_PATH", wpath),
            mock.patch.object(aw, "LAST_RUN_PATH", tmp / "last_run.json"),
            mock.patch.object(aw, "PAUSE_FLAG", tmp / "no.flag"),
            mock.patch.object(aw, "ROOT", tmp),
            mock.patch.object(aw, "load_env", return_value={}),
            mock.patch.object(aw, "find_git_exe", return_value="git"),
            mock.patch.object(aw, "send_telegram"),
            mock.patch.object(aw, "run_llm_merge", side_effect=fake_llm),
            mock.patch.object(aw.garmin_health, "fetch_recent_safe", return_value=([], None)),  # nothing new on Garmin
            mock.patch.object(aw.subprocess, "run", return_value=mock.Mock(returncode=0)),  # update_log + git
            mock.patch.object(pending_store, "PENDING_PATH", pending_path),
            mock.patch.object(pending_store, "ARCHIVE_PATH", tmp / "archive.json"),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        aw.main()
        read = lambda path: json.loads(path.read_text(encoding="utf-8"))
        return seen, read(wpath)["workouts"], read(pending_path), read(tmp / "last_run.json")

    def test_existing_entry_gets_photo_text_insights_merged(self):
        def good(ws):
            e = next(w for w in ws if w["activity_id"] == "garmin_1")
            e.update(image="boards/x.jpg", notes="squat 80kg", insights=["a", "b", "c"])

        seen, ws, pending, last = self.run_main(good)
        self.assertTrue(seen["is_existing"])  # not skipped as "no match"
        self.assertEqual(seen["match_id"], "garmin_1")
        self.assertEqual(len(ws), 2)  # no duplicate
        e = next(w for w in ws if w["activity_id"] == "garmin_1")
        self.assertEqual((e["image"], e["notes"], e["insights"]), ("boards/x.jpg", "squat 80kg", ["a", "b", "c"]))
        self.assertEqual(e["avg_hr"], 128.0)  # Garmin data intact
        self.assertEqual(pending, {})  # cleared only after success
        self.assertEqual((last["status"], last["new_workouts"]), ("ok", 0))

    def test_llm_that_duplicates_is_rolled_back_and_pending_kept(self):
        def bad(ws):
            ws.append(copy.deepcopy(next(w for w in ws if w["activity_id"] == "garmin_1")))

        seen, ws, pending, last = self.run_main(bad)
        self.assertEqual(len(ws), 2)  # rolled back
        self.assertIn(DAY, pending)  # retryable
        self.assertEqual(last["status"], "invalid_merge")


if __name__ == "__main__":
    unittest.main()
