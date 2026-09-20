"""Workout date comes from the message CONTENT (regex, no LLM), never the send time."""

import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _guard  # noqa: F401 -- installs the real-data guard

from workout_date import extract_date

# Sunday 2026-09-20, 12:44 Israel time
SENT = datetime(2026, 9, 20, 12, 44, tzinfo=ZoneInfo("Asia/Jerusalem"))
D19, D08 = date(2026, 9, 19), date(2026, 9, 8)


class ExtractDateTests(unittest.TestCase):
    def check(self, text, expected, sent=SENT):
        self.assertEqual(extract_date(text, sent), expected, text)

    def test_numeric_formats(self):
        for t in ("19/9", "19/09", "19/09/26", "19/9/2026", "19.9.26", "19.09.2026", "2026-09-19", "the 19/9 one"):
            self.check(t, D19)

    def test_month_names_english(self):
        for t in ("19 Sept", "19 september", "19th of Sep", "Sept 19", "workout of 19 Sep"):
            self.check(t, D19)

    def test_month_names_hebrew(self):
        for t in ("19 בספטמבר", "האימון היה ב-19 בספטמבר", "19 ספטמבר", "ב-8/9/26"):
            self.check(t, D19 if "19" in t else D08)

    def test_yesterday_is_relative_to_send_time(self):
        self.check("yesterday", D19)
        self.check("אתמול", D19)
        self.check("did it yesterday, 80kg", D19)
        # sent just after midnight: "yesterday" is still the previous local day
        late = datetime(2026, 9, 21, 0, 10, tzinfo=ZoneInfo("Asia/Jerusalem"))
        self.check("אתמול", date(2026, 9, 20), late)

    def test_year_is_inferred_and_never_in_the_future(self):
        # 28/12 sent on 2026-01-05 can only mean December of the previous year
        early = datetime(2026, 1, 5, 9, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        self.check("28/12", date(2025, 12, 28), early)
        self.check("22/9", None)  # after the send date

    def test_first_date_in_text_wins(self):
        self.check("15/9 thrusters 50/35kg then 19/9", date(2026, 9, 15))

    def test_reps_weights_and_times_are_not_dates(self):
        for t in (
            "First amrap with a 18 kg dumbbell \nI did 2.5 rounds\nSecond amrap with 2 15 kg dumbbells, I did 2.5 rounds",
            "thruster 50/35kg", "3 rounds of 60/40", "18.5 kg", "12:44", "9-6-3 hang squat clean",
            "2 rounds", "5/25", "31/2", "1/1", "25/12",
        ):
            self.check(t, None)

    def test_hebrew_words_are_in_logical_order_and_recognized(self):
        # Built from code points, so this holds no matter how a terminal renders RTL text.
        yesterday, day_before = "אתמול", "שלשום"
        self.assertEqual(yesterday, "אתמול")
        self.assertEqual(day_before, "שלשום")
        self.assertEqual(yesterday[0], "א")  # aleph first = logical order, not reversed
        self.assertEqual(day_before[0], "ש")  # shin first

        for text, expected in (
            (yesterday, D19),
            (day_before, date(2026, 9, 18)),
            ("אתמול", D19),
            ("שלשום", date(2026, 9, 18)),
            (f"האימון היה {yesterday} בבוקר", D19),
            (f"התאמנתי {day_before} ואז נחתי יומיים", date(2026, 9, 18)),
            ("אתמול עשיתי פרונט סקוואט 60 ק״ג ואמראפ", D19),
            ("שלשום היה אימון קשה, שלחתי את התמונה רק עכשיו", date(2026, 9, 18)),
        ):
            self.check(text, expected)

    def test_day_before_yesterday_in_english_is_not_mistaken_for_yesterday(self):
        self.check("day before yesterday", date(2026, 9, 18))
        self.check("I trained the day before yesterday", date(2026, 9, 18))
        self.check("yesterday", D19)

    def test_hebrew_relative_days_follow_the_send_time(self):
        late = datetime(2026, 9, 21, 0, 10, tzinfo=ZoneInfo("Asia/Jerusalem"))  # just after midnight
        self.check("אתמול", date(2026, 9, 20), late)
        self.check("שלשום", date(2026, 9, 19), late)

    def test_explicit_date_is_valid_up_to_30_days_back(self):
        self.check("22/8", date(2026, 8, 22))  # 29 days
        self.check("21/8", date(2026, 8, 21))  # 30 days: still valid
        self.check("20/8", None)  # 31 days: too old
        self.check("12/9", date(2026, 9, 12))  # 8 days -- beyond the plain sync's 7, but valid

    def test_no_date_at_all(self):
        self.check("", None)
        self.check("great session today", None)


if __name__ == "__main__":
    unittest.main()
