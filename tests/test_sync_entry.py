"""Shape of an entry built from a live Garmin summary: must match the DB's
existing conventions or dedup / sorting / TE silently break."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _guard  # noqa: F401 -- installs the real-data guard

import garmin_sync as gs


class NoFitClient:
    class ActivityDownloadFormat:
        ORIGINAL = "original"

    def download_activity(self, *_, **__):
        raise RuntimeError("no fit in this test")


ACT = {
    "activityId": 24342365135, "activityName": "CrossFit no-gps", "activityType": {"typeKey": "hiit"},
    "startTimeLocal": "2026-09-13 10:20:33", "duration": 2430.0, "averageHR": 124.0, "maxHR": 164.0,
    "calories": 332.0, "aerobicTrainingEffect": 2.7, "anaerobicTrainingEffect": 0.3,
}
ZONES = {"max_hr": 192, "floors": [94, 113, 134, 154, 173], "source": "test"}


class EntryShapeTests(unittest.TestCase):
    def setUp(self):
        self.e = gs._build_workout_entry(ACT, NoFitClient(), ZONES)

    def test_id_prefix_and_source(self):
        self.assertEqual((self.e["activity_id"], self.e["source"]), ("garmin_24342365135", "garmin"))

    def test_start_time_uses_db_T_format(self):
        self.assertEqual(self.e["start_time"], "2026-09-13T10:20:33")

    def test_aerobic_training_effect_is_read_from_the_right_key(self):
        self.assertEqual((self.e["training_effect"], self.e["anaerobic_training_effect"]), (2.7, 0.3))

    def test_fit_failure_still_yields_entry_without_zones(self):
        self.assertNotIn("z1_min", self.e)
        self.assertEqual((self.e["avg_hr"], self.e["max_hr"], self.e["calories"]), (124.0, 164.0, 332.0))


if __name__ == "__main__":
    unittest.main()
