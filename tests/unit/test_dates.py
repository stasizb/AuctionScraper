"""Unit tests for core.dates.normalize_auction_date.

The canonical Auction Date format across CSVs, workbook, and HTML is
'YYYY-MM-DD HH:MM UTC'. IAAI scrapes surface dates in local time with a
named timezone (e.g. 'Tue Apr 21, 8:30am CDT'), so normalization has to
convert the wall-clock time into UTC.
"""

import unittest

from tests._helpers import ROOT  # noqa: F401  (forces sys.path setup)

from core.dates import normalize_auction_date


class TestAlreadyCanonical(unittest.TestCase):
    def test_canonical_utc_unchanged(self):
        s = "2026-04-09 13:30 UTC"
        self.assertEqual(normalize_auction_date(s), s)

    def test_canonical_with_whitespace_trimmed(self):
        self.assertEqual(
            normalize_auction_date("  2026-04-09 13:30 UTC  "),
            "2026-04-09 13:30 UTC",
        )

    def test_empty_string_returns_empty(self):
        self.assertEqual(normalize_auction_date(""), "")
        self.assertEqual(normalize_auction_date("   "), "")


class TestIaaiFormatConversion(unittest.TestCase):
    def test_cdt_morning_to_utc(self):
        # CDT is UTC-5 → 8:30 CDT ⇒ 13:30 UTC
        out = normalize_auction_date("Thu Apr 09, 8:30am CDT", year=2026)
        self.assertEqual(out, "2026-04-09 13:30 UTC")

    def test_cdt_afternoon_to_utc(self):
        # 1:30pm CDT ⇒ 18:30 UTC
        out = normalize_auction_date("Thu Apr 09, 1:30pm CDT", year=2026)
        self.assertEqual(out, "2026-04-09 18:30 UTC")

    def test_minute_less_iaai_time(self):
        # Real IAAI samples like "Wed Apr 22, 11am CDT" have no minute
        out = normalize_auction_date("Wed Apr 22, 11am CDT", year=2026)
        self.assertEqual(out, "2026-04-22 16:00 UTC")

    def test_noon_am_edge_case(self):
        # 12am is midnight → 00:00 local ⇒ 05:00 UTC when CDT
        out = normalize_auction_date("Mon Apr 13, 12am CDT", year=2026)
        self.assertEqual(out, "2026-04-13 05:00 UTC")

    def test_noon_pm_edge_case(self):
        # 12pm is noon → 12:00 local ⇒ 17:00 UTC when CDT
        out = normalize_auction_date("Mon Apr 13, 12pm CDT", year=2026)
        self.assertEqual(out, "2026-04-13 17:00 UTC")

    def test_eastern_timezone(self):
        # EDT is UTC-4
        out = normalize_auction_date("Tue Apr 21, 9:00am EDT", year=2026)
        self.assertEqual(out, "2026-04-21 13:00 UTC")

    def test_pacific_timezone(self):
        # PDT is UTC-7
        out = normalize_auction_date("Wed May 01, 10:15am PDT", year=2026)
        self.assertEqual(out, "2026-05-01 17:15 UTC")

    def test_standard_time_offset(self):
        # CST is UTC-6 (no daylight savings)
        out = normalize_auction_date("Mon Jan 12, 9:00am CST", year=2026)
        self.assertEqual(out, "2026-01-12 15:00 UTC")

    def test_rollover_past_midnight(self):
        # 9pm CDT = 02:00 UTC the next day
        out = normalize_auction_date("Thu Apr 09, 9:00pm CDT", year=2026)
        self.assertEqual(out, "2026-04-10 02:00 UTC")


class TestYearHandling(unittest.TestCase):
    def test_year_param_overrides_today(self):
        out = normalize_auction_date("Tue Apr 21, 10:00am CDT", year=2030)
        self.assertTrue(out.startswith("2030-04-21"))

    def test_default_year_is_used_when_not_provided(self):
        from datetime import date
        out = normalize_auction_date("Tue Apr 21, 10:00am CDT")
        self.assertTrue(out.startswith(f"{date.today().year}-04-21"))


class TestUnknownInputs(unittest.TestCase):
    def test_garbage_string_returned_unchanged(self):
        self.assertEqual(normalize_auction_date("hello world"), "hello world")

    def test_partial_match_returned_unchanged(self):
        # Missing weekday prefix
        self.assertEqual(
            normalize_auction_date("Apr 21, 10:00am CDT"),
            "Apr 21, 10:00am CDT",
        )

    def test_unknown_timezone_keeps_wall_clock_with_label(self):
        # Unknown TZ can't be converted to UTC — preserve information by
        # formatting wall-clock time + raw TZ label.
        out = normalize_auction_date("Tue Apr 21, 9:00am WET", year=2026)
        self.assertEqual(out, "2026-04-21 09:00 WET")

    def test_invalid_month_returned_unchanged(self):
        # Regex matches shape but month isn't real
        raw = "Tue Zzz 21, 9:00am CDT"
        self.assertEqual(normalize_auction_date(raw), raw)


class TestRegression(unittest.TestCase):
    """Concrete before/after from the bug report."""

    def test_the_bug_reported_by_user(self):
        # User saw 'Mon Apr 13, 12pm CDT' alongside '2026-04-13 18:00 UTC'.
        # 12pm CDT is actually 17:00 UTC, not 18:00 — but the key point is
        # both values must converge to the SAME canonical form once this
        # function is applied to the IAAI side.
        canonical = "2026-04-13 17:00 UTC"
        from_iaai = normalize_auction_date("Mon Apr 13, 12pm CDT", year=2026)
        from_copart_like = normalize_auction_date(canonical)
        self.assertEqual(from_iaai, canonical)
        self.assertEqual(from_copart_like, canonical)

    def test_idempotent(self):
        # Running the normalizer twice must be a no-op (workbook self-heal
        # pass depends on this).
        raw   = "Thu Apr 09, 8:30am CDT"
        once  = normalize_auction_date(raw, year=2026)
        twice = normalize_auction_date(once,  year=2026)
        self.assertEqual(once, twice)


if __name__ == "__main__":
    unittest.main()
