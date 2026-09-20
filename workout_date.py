#!/usr/bin/env python3
"""
Finds the WORKOUT date in a free-text message -- plain regex, no LLM.

The date must come from the content the user sent, never from when it was
sent. Recognized: DD/MM, DD/MM/YY(YY), DD.MM.YY(YY), YYYY-MM-DD, "19 Sept",
"Sept 19", Hebrew month names ("19 בספטמבר"), "yesterday"/"אתמול" and
"day before yesterday"/"שלשום" (relative to the message send time). Bare DD.MM is NOT accepted: it collides
with weights like "18.5 kg".
"""

import re
from datetime import date, timedelta
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Jerusalem")
MAX_AGE_DAYS = 30  # an explicit workout date is valid up to this far back; future dates never

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
    "ינואר": 1, "ינו": 1, "פברואר": 2, "פבר": 2, "מרץ": 3, "מרס": 3, "אפריל": 4, "אפר": 4,
    "מאי": 5, "יוני": 6, "יולי": 7, "אוגוסט": 8, "אוג": 8, "ספטמבר": 9, "ספט": 9,
    "אוקטובר": 10, "אוק": 10, "נובמבר": 11, "נוב": 11, "דצמבר": 12, "דצמ": 12,
}
_NAMES = "|".join(sorted(map(re.escape, MONTHS), key=len, reverse=True))

_ISO = re.compile(r"(?<!\d)(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})(?!\d)")
_SLASH = re.compile(r"(?<![\d/.])(?P<d>\d{1,2})/(?P<m>\d{1,2})(?:/(?P<y>\d{4}|\d{2}))?(?![\d/])")
_DOT = re.compile(r"(?<![\d/.])(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4}|\d{2})(?!\d)")
# Optional Hebrew prefix (ב/ל/מ) glued to the month, e.g. "בספטמבר".
_DAY_MONTH = re.compile(
    rf"(?<![\d/.])(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\.?\s*(?:of\s+)?[בלמ]?(?P<mon>{_NAMES})\b[.׳']?"
    rf"(?:\s*,?\s*(?P<y>\d{{4}}))?",
    re.IGNORECASE,
)
_MONTH_DAY = re.compile(
    rf"\b(?P<mon>{_NAMES})\b\.?\s*(?P<d>\d{{1,2}})(?:st|nd|rd|th)?(?![\d:/.])"
    rf"(?:\s*,?\s*(?P<y>\d{{4}}))?",
    re.IGNORECASE,
)
_YESTERDAY = re.compile(r"\byesterday\b|(?<!\w)אתמול(?!\w)", re.IGNORECASE)
_DAY_BEFORE = re.compile(r"\bday before yesterday\b|(?<!\w)שלשום(?!\w)", re.IGNORECASE)


def is_plausible(d, ref):
    """True if `d` is on/before `ref` and at most MAX_AGE_DAYS earlier."""
    return 0 <= (ref - d).days <= MAX_AGE_DAYS


def _make(day, month, year, ref):
    """Build a date; a missing year means the latest such date not after `ref`."""
    try:
        if year is None:
            cand = date(ref.year, month, day)
            if cand > ref:
                cand = date(ref.year - 1, month, day)
        else:
            y = int(year)
            cand = date(y + 2000 if y < 100 else y, month, day)
    except ValueError:
        return None
    return cand if is_plausible(cand, ref) else None


def extract_date(text, sent_at):
    """First plausible workout date mentioned in `text`, or None.
    `sent_at` is the message's aware datetime (relative words + year inference)."""
    ref = sent_at.astimezone(LOCAL_TZ).date()
    found = []  # (position in text, date)

    for rx in (_ISO, _SLASH, _DOT):
        for m in rx.finditer(text):
            d = _make(int(m["d"]), int(m["m"]), m["y"], ref)
            if d:
                found.append((m.start(), d))
    for rx in (_DAY_MONTH, _MONTH_DAY):
        for m in rx.finditer(text):
            d = _make(int(m["d"]), MONTHS[m["mon"].lower()], m["y"], ref)
            if d:
                found.append((m.start(), d))
    # "day before yesterday" also contains "yesterday"; the earlier position wins below.
    for m in _YESTERDAY.finditer(text):
        found.append((m.start(), ref - timedelta(days=1)))
    for m in _DAY_BEFORE.finditer(text):
        found.append((m.start(), ref - timedelta(days=2)))

    return min(found)[1] if found else None
