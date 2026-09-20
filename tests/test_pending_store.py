"""pending_store: submissions grouped by content, workout date never taken from send time."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _guard  # noqa: F401 -- installs the real-data guard

import pending_store as ps
import support

NOW = datetime.now(ps.LOCAL_TZ).replace(microsecond=0)
YESTERDAY = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
YESTERDAY_DDMM = (NOW - timedelta(days=1)).strftime("%d/%m")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        support.patch_pending(self, self.tmp)


class GroupingTests(Base):
    def test_photo_then_text_are_one_submission(self):
        eid = ps.add_photo(NOW, "boards/a.jpg")
        eid2, kind, wdate = ps.add_text(NOW + timedelta(minutes=3), "squat 80kg")
        self.assertEqual((eid2, kind, wdate), (eid, "stored", None))
        (only,) = ps.entries()
        self.assertEqual((only[1]["photo"], only[1]["texts"]), ("boards/a.jpg", ["squat 80kg"]))

    def test_send_time_is_never_used_as_the_workout_date(self):
        ps.add_photo(NOW, "boards/a.jpg")
        ps.add_text(NOW, "First amrap with a 18 kg dumbbell, I did 2.5 rounds")
        (_, e), = ps.entries()
        self.assertIsNone(e["workout_date"])

    def test_date_in_text_sets_the_workout_date(self):
        _, _, wdate = ps.add_text(NOW, f"{YESTERDAY_DDMM} - amrap 20min")
        self.assertEqual(wdate, YESTERDAY)
        self.assertEqual(ps.entries()[0][1]["date_source"], "text")

    def test_messages_hours_apart_are_separate_submissions(self):
        ps.add_photo(NOW, "boards/a.jpg")
        ps.add_text(NOW + timedelta(hours=3), "another workout")
        self.assertEqual(len(ps.entries()), 2)

    def test_second_photo_starts_a_new_submission(self):
        ps.add_photo(NOW, "boards/a.jpg")
        ps.add_photo(NOW + timedelta(minutes=1), "boards/b.jpg")
        self.assertEqual(len(ps.entries()), 2)

    def test_a_different_text_date_means_a_different_workout(self):
        ps.add_text(NOW, "19/9 amrap")
        ps.add_text(NOW + timedelta(minutes=1), "15/9 emom")
        self.assertEqual([e["workout_date"][5:] for _, e in ps.entries()], ["09-19", "09-15"])


class DateReplyTests(Base):
    def setUp(self):
        super().setUp()
        self.eid = ps.add_photo(NOW - timedelta(days=1), "boards/a.jpg")
        ps.update(self.eid, asked_date=True)

    def test_short_date_reply_dates_the_asked_entry_and_is_not_stored_as_text(self):
        eid, kind, wdate = ps.add_text(NOW, YESTERDAY_DDMM)
        self.assertEqual((eid, kind, wdate), (self.eid, "date_reply", YESTERDAY))
        e = ps.load()[self.eid]
        self.assertEqual((e["texts"], e["date_source"]), ([], "user_reply"))

    def test_yesterday_in_a_reply_is_relative_to_the_reply_time(self):
        _, kind, wdate = ps.add_text(NOW, "אתמול")
        self.assertEqual((kind, wdate), ("date_reply", YESTERDAY))

    def test_long_recap_with_a_date_is_not_a_date_reply(self):
        text = f"{YESTERDAY_DDMM}: front squat 60kg x8, then 15-12-9 thrusters, I did it in 12 minutes"
        _, kind, _ = ps.add_text(NOW, text)
        self.assertEqual(kind, "stored")
        self.assertIsNone(ps.load()[self.eid]["workout_date"])

    def test_reply_goes_to_the_oldest_asked_entry(self):
        newer = ps.add_photo(NOW - timedelta(hours=5), "boards/b.jpg")
        ps.update(newer, asked_date=True)
        eid, _, _ = ps.add_text(NOW, YESTERDAY_DDMM)
        self.assertEqual(eid, self.eid)


class PruneTests(Base):
    def write(self, **entries):
        ps.save(entries)

    def test_dated_entry_is_archived_only_when_the_workout_is_older_than_30_days(self):
        self.write(A=support.entry(workout_date=support.days_ago(31), source="text", texts=["x"]),
                   B=support.entry(workout_date=support.days_ago(30), source="text"),
                   C=support.entry(workout_date=support.days_ago(12), source="text"))  # 12d: old for the plain sync, fine here
        pruned = ps.prune_stale()
        self.assertEqual([k for k, _ in pruned], ["A"])
        self.assertEqual(sorted(ps.load()), ["B", "C"])
        self.assertIn("A", json.loads(ps.ARCHIVE_PATH.read_text(encoding="utf-8")))  # archived, not lost

    def test_undated_entry_is_archived_30_days_after_the_message_date(self):
        now = datetime.now(ps.LOCAL_TZ)
        self.write(U=support.entry(sent_at=now - timedelta(days=29)), V=support.entry(sent_at=now - timedelta(days=31)))
        pruned = ps.prune_stale()
        self.assertEqual([k for k, _ in pruned], ["V"])
        self.assertEqual(list(ps.load()), ["U"])


class MigrationAndPhotoTests(Base):
    def test_old_date_keyed_shape_loads_as_undated(self):
        ps.PENDING_PATH.write_text(json.dumps({
            "2026-09-20": {"photo": "boards/2026-09-20.jpg", "texts": ["hello"], "received_at": "2026-09-20T12:50:04.949960"},
        }), encoding="utf-8")
        ((eid, e),) = ps.entries()
        self.assertEqual((e["photo"], e["texts"], e["workout_date"]), ("boards/2026-09-20.jpg", ["hello"], None))
        self.assertEqual(eid, "2026-09-20T12:50:04")

    def test_finalize_photo_renames_to_workout_date_without_clobbering(self):
        eid = ps.add_photo(NOW, "boards/2026-09-20_124100.jpg")
        (self.tmp / "boards" / "2026-09-20_124100.jpg").write_bytes(b"new")
        (self.tmp / "boards" / "2026-09-19.jpg").write_bytes(b"already there")
        self.assertEqual(ps.finalize_photo(eid, "2026-09-19"), "boards/2026-09-19_2.jpg")
        self.assertEqual((self.tmp / "boards" / "2026-09-19.jpg").read_bytes(), b"already there")
        self.assertEqual((self.tmp / "boards" / "2026-09-19_2.jpg").read_bytes(), b"new")
        self.assertEqual(ps.load()[eid]["photo"], "boards/2026-09-19_2.jpg")


if __name__ == "__main__":
    unittest.main()
