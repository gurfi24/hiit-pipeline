"""HR-zone thresholds: source priority, no BIRTH_DATE leak, FIT/ZIP sniffing."""

import io
import sys
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _guard  # noqa: F401 -- installs the real-data guard

import garmin_sync as gs

GARMIN_ZONES = [{
    "trainingMethod": "HR_MAX", "sport": "DEFAULT", "maxHeartRateUsed": 192,
    "zone1Floor": 94, "zone2Floor": 113, "zone3Floor": 134, "zone4Floor": 154, "zone5Floor": 173,
}]


class FakeClient:
    def __init__(self, zones=None, fail=False):
        self.zones, self.fail = zones, fail

    def connectapi(self, path):
        if self.fail:
            raise RuntimeError("boom")
        return self.zones

    def get_user_summary(self, *_):  # that is the DAY's max HR -- must never drive zone thresholds
        raise AssertionError("get_user_summary must not be used for zone thresholds")


class ResolveTests(unittest.TestCase):
    def test_prefers_garmin_zone_settings(self):
        z = gs.resolve_hr_zones(FakeClient(GARMIN_ZONES), workouts=[{"max_hr": 250}], env={"BIRTH_DATE": "1990-01-01"})
        self.assertEqual((z["source"], z["max_hr"], z["floors"]), ("garmin zone settings", 192, [94, 113, 134, 154, 173]))

    def test_falls_back_to_observed_max(self):
        z = gs.resolve_hr_zones(FakeClient(fail=True), workouts=[{"max_hr": 169}, {"max_hr": 181}], env={})
        self.assertEqual((z["max_hr"], z["floors"][0]), (181, 90))
        self.assertIn("observed", z["source"])

    def test_age_fallback_computed_at_runtime_and_birth_date_not_printed(self):
        birth = (datetime.now() - timedelta(days=365.25 * 34 + 30)).strftime("%Y-%m-%d")
        buf = io.StringIO()
        with redirect_stdout(buf):
            z = gs.resolve_hr_zones(FakeClient(fail=True), workouts=[], env={"BIRTH_DATE": birth})
        self.assertEqual(z["max_hr"], 220 - 34)
        self.assertIn("age formula", z["source"])
        self.assertNotIn(birth, buf.getvalue())
        self.assertNotIn(birth, repr(z))

    def test_last_resort_is_default_constant(self):
        z = gs.resolve_hr_zones(FakeClient(fail=True), workouts=[], env={})
        self.assertEqual((z["max_hr"], z["source"]), (gs.DEFAULT_MAX_HR, "default constant"))


class ZoneMinuteTests(unittest.TestCase):
    def test_minutes_use_given_floors(self):
        t0 = datetime(2026, 9, 19, 10, 0, 0)
        # 60s in each of z0..z5 (90, 94 = exactly the Z1 floor, 120, 140, 160, 180), then a closing sample
        hrs = [90, 94, 120, 140, 160, 180, 100]
        records = [(t0 + timedelta(seconds=60 * i), hr) for i, hr in enumerate(hrs)]
        m = gs.minutes_per_zone(records, [94, 113, 134, 154, 173])
        self.assertEqual([m[f"z{z}_min"] for z in range(6)], [1.0] * 6)


class FitPayloadTests(unittest.TestCase):
    FIT = b"\x0e\x10\x00\x00\x00\x00\x00\x00.FIT\x00\x00"

    def test_zip_is_extracted(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("123_ACTIVITY.fit", self.FIT)
        self.assertEqual(gs._extract_fit_bytes(buf.getvalue()), self.FIT)

    def test_bare_fit_passes_through(self):
        self.assertEqual(gs._extract_fit_bytes(self.FIT), self.FIT)

    def test_garbage_and_fitless_zip_raise(self):
        with self.assertRaises(ValueError):
            gs._extract_fit_bytes(b"<html>nope</html>")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.txt", "x")
        with self.assertRaises(ValueError):
            gs._extract_fit_bytes(buf.getvalue())


if __name__ == "__main__":
    unittest.main()
