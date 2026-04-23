"""Date-string normalization shared across scripts.

The canonical Auction Date format across CSVs, workbook, and HTML is:

    YYYY-MM-DD HH:MM UTC

Copart's scraper produces this format directly (it already has a real UTC
timestamp in the API response).  IAAI's listing page renders dates like
"Tue Apr 21, 8:30am CDT" — local time in a named US timezone.
`normalize_auction_date()` converts the IAAI form to UTC so both sources
land in the same canonical form, and leaves already-canonical strings
untouched.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

# "Tue Apr 21, 8:30am CDT"   — minute present
# "Wed Apr 22, 11am CDT"     — minute absent
_IAAI_DATE_RE = re.compile(
    r'^\w{3}\s+(\w{3})\s+(\d{1,2}),\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s+(\w+)$',
    re.IGNORECASE,
)

# Already-canonical: "2026-04-21 18:00 UTC"
_CANONICAL_RE = re.compile(r'^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+UTC$')

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Hours west of UTC (positive = behind UTC). Common US-auction timezones.
# DST and standard variants are both listed since IAAI surfaces whichever
# applies on the auction date.
_TZ_OFFSETS_HOURS = {
    "UTC": 0,  "GMT": 0,
    "EDT": 4,  "EST": 5,
    "CDT": 5,  "CST": 6,
    "MDT": 6,  "MST": 7,
    "PDT": 7,  "PST": 8,
    "AKDT": 8, "AKST": 9,
    "HDT":  9, "HST": 10,
}


def normalize_auction_date(value: str, year: int | None = None) -> str:
    """Return `value` converted to 'YYYY-MM-DD HH:MM UTC'.

    * Strings already in the canonical form pass through unchanged.
    * IAAI-style strings ('Tue Apr 21, 8:30am CDT', '11am EDT', …) are
      parsed, converted to UTC using the timezone label, and reformatted.
    * Strings in neither form are returned unchanged (so the caller never
      loses information to an over-eager regex).

    `year` defaults to the current year — IAAI listings never include a year.
    """
    s = (value or "").strip()
    if not s or _CANONICAL_RE.match(s):
        return s

    m = _IAAI_DATE_RE.match(s)
    if not m:
        return s

    month_s, day_s, hour_s, minute_s, ampm, tz = m.groups()
    month = _MONTH_MAP.get(month_s.lower())
    if month is None:
        return s

    hour = int(hour_s)
    if ampm.lower() == "pm" and hour != 12:
        hour += 12
    elif ampm.lower() == "am" and hour == 12:
        hour = 0
    minute = int(minute_s) if minute_s else 0

    try:
        local_dt = datetime(year or date.today().year, month, int(day_s), hour, minute)
    except ValueError:
        return s

    offset_h = _TZ_OFFSETS_HOURS.get(tz.upper())
    if offset_h is None:
        # Unknown timezone — keep the local wall-clock time but flag it
        # clearly with the original label so the user can spot the gap.
        return local_dt.strftime("%Y-%m-%d %H:%M ") + tz.upper()

    utc_dt = local_dt + timedelta(hours=offset_h)
    return utc_dt.strftime("%Y-%m-%d %H:%M UTC")
