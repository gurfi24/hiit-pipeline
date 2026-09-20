"""The guard itself: tests must be unable to touch the real data/ folder.

Destructive actions are probed on NON-EXISTENT names inside the real dirs and
cleaned up in `finally`, so even a broken guard cannot damage real data here."""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _guard

import analyze_workout as aw
import garmin_health
import garmin_sync
import pending_store
import telegram_bot

REAL_DATA = _guard.REPO / "data"
REAL_BOARDS = _guard.REPO / "boards"
PROBE = REAL_DATA / "__guard_probe__"
PROBE2 = REAL_DATA / "__guard_probe2__"
BOARD_PROBE = REAL_BOARDS / "__guard_probe__.jpg"


def cleanup(*paths):
    """Remove anything a broken guard let through (uses the unguarded originals)."""
    for p in paths:
        if os.path.isdir(p):
            _guard.ORIG["rmdir"](p)
        elif os.path.exists(p):
            _guard.ORIG["unlink"](p)


class GuardTests(unittest.TestCase):
    def test_every_app_data_path_points_at_the_temp_dir(self):
        paths = [
            pending_store.PENDING_PATH, pending_store.ARCHIVE_PATH, pending_store.BOARDS_DIR,
            aw.WORKOUTS_PATH, aw.LAST_RUN_PATH, aw.PAUSE_FLAG,
            telegram_bot.STATE_PATH, telegram_bot.LAST_RUN_PATH, telegram_bot.BOARDS_DIR, telegram_bot.PAUSE_FLAG,
            garmin_health.STATE_PATH, garmin_sync.DATA_DIR, aw.LOCK_PATH, telegram_bot.HEARTBEAT_PATH,
        ]
        for p in paths:
            self.assertTrue(str(Path(p).resolve()).startswith(str(_guard.TMP.resolve())), p)

    def test_reading_the_real_data_dir_fails_loudly(self):
        real = REAL_DATA / "workouts.json"  # read-only probes on a real file
        for action in (
            lambda: open(real, encoding="utf-8"),
            lambda: open(file=str(real)),
            lambda: real.open(),
            lambda: real.read_text(encoding="utf-8"),
            lambda: real.read_bytes(),
            lambda: real.exists(),
            lambda: real.stat(),
            lambda: list(REAL_DATA.iterdir()),
            lambda: list(REAL_DATA.glob("*.json")),
            lambda: os.listdir(REAL_DATA),
            lambda: BOARD_PROBE.exists(),
            lambda: (_guard.REPO / "automation_paused.flag").exists(),
        ):
            with self.assertRaises(_guard.RealDataAccessError):
                action()

    def test_writing_the_real_data_dir_fails_loudly(self):
        try:
            for action in (
                lambda: open(PROBE, "w"),
                lambda: PROBE.open("w"),
                lambda: PROBE.write_text("x", encoding="utf-8"),
                lambda: PROBE.write_bytes(b"x"),
                lambda: PROBE.touch(),
                lambda: PROBE.mkdir(),
                lambda: BOARD_PROBE.write_bytes(b"x"),
                lambda: PROBE.replace(PROBE2),
                lambda: PROBE.rename(PROBE2),
                lambda: (_guard.TMP / "nope").replace(PROBE2),  # real path as the target
                lambda: PROBE.unlink(),
                lambda: os.replace(PROBE, PROBE2),
                lambda: os.makedirs(PROBE),
            ):
                with self.assertRaises(_guard.RealDataAccessError):
                    action()
        finally:
            cleanup(PROBE, PROBE2, BOARD_PROBE)

    def test_the_error_cannot_be_swallowed_by_except_exception(self):
        self.assertFalse(issubclass(_guard.RealDataAccessError, Exception))
        with self.assertRaises(_guard.RealDataAccessError):
            try:
                (REAL_DATA / "pending.json").exists()
            except Exception:  # noqa: BLE001 -- the production pattern the guard must beat
                pass

    def test_temp_paths_and_repo_code_stay_accessible(self):
        f = _guard.TMP_DATA / "ok.txt"
        f.write_text("hi", encoding="utf-8")
        self.assertEqual(f.read_text(encoding="utf-8"), "hi")
        self.assertIn("RealDataAccessError", (_guard.REPO / "tests" / "_guard.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
