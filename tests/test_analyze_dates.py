"""/start: the workout date comes from content (text, then photo), is never guessed,
and the whole run stays within the $0.50 cap. No network; LLM/vision/Garmin faked."""

import io
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _guard  # noqa: F401 -- installs the real-data guard

import analyze_workout as aw
import support
from support import days_ago, entry, run_analyze, workout

TODAY, D3, D1 = days_ago(0), days_ago(3), days_ago(1)


class AskForDateTests(unittest.TestCase):
    def test_undated_text_only_asks_once_and_does_nothing_else(self):
        p = {"E": entry(texts=["First amrap with a 18 kg dumbbell, I did 2.5 rounds"])}
        r = run_analyze(self, p, [workout(1, TODAY)], activities=[workout(2, TODAY)])
        self.assertEqual(len(r.calls.telegram), 1)
        self.assertIn("📅", r.calls.telegram[0])
        self.assertEqual((r.calls.llm, r.calls.vision, r.calls.garmin), ([], [], 0))  # no cost, no Garmin call
        self.assertTrue(r.pending["E"]["asked_date"])  # stays queued, awaiting the reply
        self.assertIsNone(r.pending["E"]["workout_date"])
        self.assertEqual(r.last_run["status"], "awaiting_date")

    def test_send_date_is_not_a_fallback_even_when_garmin_has_that_day(self):
        # Sent today, Garmin has a workout today, but the content gives no date -> must NOT match today.
        p = {"E": entry(photo=None, texts=["amrap results"])}
        r = run_analyze(self, p, [], activities=[workout(2, TODAY)])
        self.assertEqual(r.calls.llm, [])
        self.assertIsNone(r.pending["E"]["workout_date"])

    def test_workout_date_from_text_not_send_date_drives_the_match(self):
        # Sent today about a workout 3 days ago; Garmin only has today's -> no match for the real date.
        p = {"E": entry(workout_date=D3, source="text", texts=["x"])}
        r = run_analyze(self, p, [], activities=[workout(2, TODAY)])
        self.assertEqual(r.calls.llm, [])
        self.assertEqual(r.last_run["status"], "no_match")
        self.assertIn(D3, r.calls.telegram[0])
        self.assertIn("E", r.pending)


class PhotoDateTests(unittest.TestCase):
    def test_photo_date_used_when_text_has_none(self):
        p = {"E": entry(photo="boards/x.jpg", texts=["amrap"])}
        r = run_analyze(self, p, [], llm_edit=None, vision=lambda ph: (datetime.strptime(D3, "%Y-%m-%d").date(), 0.03, True),
                        activities=[workout(7, D3)])
        (call,) = r.calls.llm
        self.assertEqual((call.date, r.calls.vision), (D3, ["boards/x.jpg"]))
        self.assertIn("מהתמונה", r.calls.telegram[-1])  # the summary says where the date came from

    def test_text_date_beats_photo_and_skips_the_vision_call(self):
        p = {"E": entry(workout_date=D1, source="text", photo="boards/x.jpg", texts=["x"])}
        r = run_analyze(self, p, [], activities=[workout(7, D1)])
        self.assertEqual((r.calls.vision, len(r.calls.llm)), ([], 1))

    def test_photo_without_a_date_asks_and_is_not_read_again(self):
        p = {"E": entry(photo="boards/x.jpg")}
        r = run_analyze(self, p, [], vision=lambda ph: (None, 0.02, True))
        self.assertEqual(len(r.calls.vision), 1)
        self.assertTrue(r.pending["E"]["photo_date_checked"])
        self.assertIn("📅", r.calls.telegram[0])
        # a second /start must not pay for the same photo again
        r2 = run_analyze(self, r.pending, [], vision=lambda ph: (None, 0.02, True))
        self.assertEqual(r2.calls.vision, [])

    def test_failed_vision_call_is_retried_next_time(self):
        p = {"E": entry(photo="boards/x.jpg")}
        r = run_analyze(self, p, [], vision=lambda ph: (None, 0.0, False))
        self.assertFalse(r.pending["E"]["photo_date_checked"])
        self.assertIn("📅", r.calls.telegram[0])


class BudgetTests(unittest.TestCase):
    def test_vision_spend_is_deducted_from_the_main_call_budget(self):
        p = {"E": entry(photo="boards/x.jpg", texts=["x"])}
        d3 = datetime.strptime(D3, "%Y-%m-%d").date()
        r = run_analyze(self, p, [], vision=lambda ph: (d3, 0.03, True), activities=[workout(7, D3)])
        self.assertAlmostEqual(r.calls.llm[0].budget, 0.47)
        self.assertAlmostEqual(r.last_run["cost_usd"], 0.13)  # 0.03 vision + 0.10 fake main call


class MultiEntryTests(unittest.TestCase):
    def test_same_workout_date_entries_are_combined_into_one_run(self):
        now = datetime.now(support.LOCAL_TZ)
        p = {
            "A": entry(sent_at=now - timedelta(hours=5), workout_date=D1, source="text", texts=["front squat 60kg"]),
            "B": entry(sent_at=now, workout_date=D1, source="user_reply", photo="boards/x.jpg", texts=["amrap 2.5 rounds"]),
        }
        r = run_analyze(self, p, [], llm_edit=None, activities=[workout(7, D1)])
        (call,) = r.calls.llm
        self.assertEqual(call.entry["texts"], ["front squat 60kg", "amrap 2.5 rounds"])
        self.assertEqual(r.pending, {})

    def test_up_to_three_dates_newest_first_each_with_its_own_budget_rest_listed(self):
        days = [days_ago(n) for n in (1, 2, 3, 4)]  # newest first
        p = {f"E{n}": entry(workout_date=d, source="text", texts=[f"t{n}"]) for n, d in enumerate(days)}
        r = run_analyze(self, p, [], activities=[workout(10 + n, d) for n, d in enumerate(days)])
        self.assertEqual([c.date for c in r.calls.llm], days[:3])  # newest first, max 3
        self.assertEqual([c.budget for c in r.calls.llm], [0.5, 0.5, 0.5])  # a full cap for every run
        self.assertEqual(len(r.calls.telegram), 3)  # one reply per run
        self.assertEqual(list(r.pending), ["E3"])  # the 4th stays queued
        oldest = datetime.strptime(days[3], "%Y-%m-%d").strftime("%d/%m")
        self.assertNotIn(oldest, r.calls.telegram[0] + r.calls.telegram[1])
        self.assertIn(oldest, r.calls.telegram[2])  # the remaining ones are listed in the last reply
        self.assertEqual((r.last_run["status"], r.last_run["new_workouts"]), ("ok", 3))
        self.assertAlmostEqual(r.last_run["cost_usd"], 0.3)  # 3 x 0.10 fake main calls
        self.assertEqual(len(r.workouts), 3)

    def test_a_vision_read_only_reduces_the_budget_of_its_own_run(self):
        d1, d2 = days_ago(1), days_ago(2)
        p = {
            "A": entry(workout_date=d1, source="text", texts=["a"]),
            "B": entry(photo="boards/b.jpg", texts=["b"]),  # dated by the photo read
        }
        found = datetime.strptime(d2, "%Y-%m-%d").date()
        r = run_analyze(self, p, [], vision=lambda ph: (found, 0.03, True), activities=[workout(1, d1), workout(2, d2)])
        self.assertEqual({c.date: c.budget for c in r.calls.llm}, {d1: 0.5, d2: 0.47})

    def test_one_failed_run_does_not_stop_the_next_ones(self):
        d1, d2 = days_ago(1), days_ago(2)
        p = {"A": entry(workout_date=d1, source="text"), "B": entry(workout_date=d2, source="text")}
        r = run_analyze(self, p, [], activities=[workout(2, d2)])  # nothing on Garmin for d1 yet
        self.assertEqual([c.date for c in r.calls.llm], [d2])
        self.assertEqual(list(r.pending), ["A"])
        self.assertEqual(r.last_run["status"], "partial")


class LookbackAndWindowTests(unittest.TestCase):
    def garmin_days(self, r):
        (args, kwargs), = r.calls.garmin_args
        return args[0], kwargs

    def test_recent_dates_use_the_plain_seven_day_pull(self):
        r = run_analyze(self, {"E": entry(workout_date=days_ago(2), source="text")}, [], activities=[workout(1, days_ago(2))])
        days, kwargs = self.garmin_days(r)
        self.assertEqual(days, 7)
        self.assertEqual(kwargs["only_dates"], {days_ago(2)})  # FIT downloads only for the needed day

    def test_an_older_explicit_date_widens_the_lookback_up_to_thirty_days(self):
        d = days_ago(25)
        r = run_analyze(self, {"E": entry(workout_date=d, source="text", texts=["x"])}, [], activities=[workout(1, d)])
        days, _ = self.garmin_days(r)
        self.assertEqual(days, 26)
        self.assertEqual(r.calls.llm[0].date, d)  # a 25-day-old date is still processed
        d30 = days_ago(30)
        r = run_analyze(self, {"E": entry(workout_date=d30, source="text")}, [], activities=[workout(1, d30)])
        self.assertEqual(self.garmin_days(r)[0], 31)  # covers all of the 30th day
        self.assertEqual(len(r.calls.llm), 1)

    def test_already_stored_activities_are_not_downloaded_again(self):
        d = days_ago(2)
        r = run_analyze(self, {"E": entry(workout_date=d, source="text")}, [workout(5, d)], activities=[])
        self.assertEqual(self.garmin_days(r)[1]["known_ids"], {"garmin_5"})

    def test_dated_entries_are_archived_only_after_thirty_days_and_announced(self):
        p = {
            "OLD": entry(workout_date=days_ago(31), source="text", texts=["x"]),
            "OK": entry(workout_date=days_ago(29), source="text", texts=["y"]),
        }
        r = run_analyze(self, p, [], activities=[workout(1, days_ago(29))])
        self.assertIn("OLD", r.archive)
        self.assertNotIn("OK", r.archive)
        self.assertTrue(any("🗄️" in m for m in r.calls.telegram))
        self.assertEqual([c.date for c in r.calls.llm], [days_ago(29)])

    def test_undated_entries_are_archived_thirty_days_after_the_message_date(self):
        now = datetime.now(support.LOCAL_TZ)
        p = {"OLD": entry(sent_at=now - timedelta(days=31)), "NEW": entry(sent_at=now - timedelta(days=29))}
        r = run_analyze(self, p, [], activities=[])
        self.assertEqual(list(r.archive), ["OLD"])
        self.assertEqual(list(r.pending), ["NEW"])
        self.assertTrue(any("🗄️" in m for m in r.calls.telegram))

    def test_archive_print_survives_a_cp1252_stdout(self):
        # A Windows console/pipe is cp1252: printing Hebrew/emoji labels used to
        # raise UnicodeEncodeError before any analysis ran. Simulate that stream
        # explicitly so the test fails on any machine if the fix is removed.
        now = datetime.now(support.LOCAL_TZ)
        p = {"OLD": entry(sent_at=now - timedelta(days=31))}
        out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        err = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err),                 mock.patch.dict(os.environ):
            r = run_analyze(self, p, [], activities=[])
            self.assertEqual(os.environ["PYTHONIOENCODING"], "utf-8")  # children inherit it
        self.assertEqual(list(r.archive), ["OLD"])
        out.flush()
        self.assertIn("Archived stale pending entries", out.buffer.getvalue().decode("utf-8"))


class RunLockTests(unittest.TestCase):
    def setUp(self):
        self.p = {"E": entry(workout_date=D1, source="text", texts=["b"])}

    def test_a_second_run_while_one_is_active_does_nothing_and_keeps_the_lock(self):
        r = run_analyze(self, self.p, [], activities=[workout(7, D1)], lock_age_s=60)
        self.assertEqual((r.calls.llm, r.calls.garmin, r.calls.telegram), ([], 0, []))  # no cost, no noise
        self.assertIn("E", r.pending)  # stays queued
        self.assertTrue(r.lock_exists)  # the loser must not release the winner's lock

    def test_a_stale_lock_from_a_crashed_run_is_replaced(self):
        r = run_analyze(self, self.p, [], activities=[workout(7, D1)], lock_age_s=aw.LOCK_STALE_S + 60)
        self.assertEqual(len(r.calls.llm), 1)
        self.assertFalse(r.lock_exists)  # released at the end

    def test_the_lock_is_released_after_a_normal_run(self):
        r = run_analyze(self, self.p, [], activities=[workout(7, D1)])
        self.assertEqual(len(r.calls.llm), 1)
        self.assertFalse(r.lock_exists)

    def test_the_lock_is_released_even_if_the_run_crashes(self):
        lock = support.Path(support.tempfile.mkdtemp()) / "analyze.lock"
        with mock.patch.object(aw, "LOCK_PATH", lock), mock.patch.object(aw, "_main", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                aw.main()
        self.assertFalse(lock.exists())


class MoreMultiEntryTests(unittest.TestCase):

    def test_dated_entry_is_processed_and_the_undated_one_is_asked_about(self):
        p = {
            "DATED": entry(workout_date=D1, source="text", texts=["b"]),
            "UNDATED": entry(texts=["no date here"]),
        }
        r = run_analyze(self, p, [], activities=[workout(7, D1)])
        self.assertEqual(len(r.calls.llm), 1)
        self.assertEqual(list(r.pending), ["UNDATED"])
        self.assertTrue(r.pending["UNDATED"]["asked_date"])
        self.assertEqual(sum("📅 לא מצאתי" in m for m in r.calls.telegram), 1)


class ReadPhotoDateTests(unittest.TestCase):
    REF = datetime.now(support.LOCAL_TZ).date()

    def call(self, result_text, is_error=False, cost=0.02, returncode=0):
        out = json.dumps({"result": result_text, "total_cost_usd": cost, "is_error": is_error})
        with mock.patch.object(aw.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], returncode, stdout=out, stderr="")) as run:
            return aw.read_photo_date("boards/x.jpg", self.REF), run.call_args[0][0]

    def test_returns_the_date_and_uses_a_tiny_capped_readonly_call(self):
        target = self.REF - timedelta(days=2)
        (found, cost, ok), cmd = self.call(target.isoformat())
        self.assertEqual((found, cost, ok), (target, 0.02, True))
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "0.05")
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], "Read")
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")
        self.assertNotIn("bypassPermissions", cmd)

    def test_null_garbage_and_implausible_dates_all_mean_no_date(self):
        for text in ("null", "I see a whiteboard", "2026-13-45", (self.REF + timedelta(days=5)).isoformat(),
                     (self.REF - timedelta(days=400)).isoformat()):
            (found, _, ok), _ = self.call(text)
            self.assertEqual((found, ok), (None, True), text)

    def test_error_or_over_budget_is_a_failed_read(self):
        (found, cost, ok), _ = self.call("x", is_error=True, cost=0.05)
        self.assertEqual((found, ok, cost), (None, False, 0.05))


if __name__ == "__main__":
    unittest.main()
