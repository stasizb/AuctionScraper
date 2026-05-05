"""Unit tests for core/filters.py."""

import unittest
from datetime import date

from tests._helpers import ROOT  # noqa: F401

from core.filters import apply_age_filter


class TestApplyAgeFilter(unittest.TestCase):
    def test_age_sets_year_min_and_drops_year_max(self):
        out = apply_age_filter({"age": 2}, today=date(2026, 4, 24))
        self.assertEqual(out["year_min"], 2024)
        self.assertNotIn("year_max", out)
        self.assertNotIn("age", out)

    def test_age_overrides_explicit_year_min_and_year_max(self):
        out = apply_age_filter(
            {"age": 3, "year_min": 2010, "year_max": 2015},
            today=date(2026, 4, 24),
        )
        self.assertEqual(out["year_min"], 2023)
        self.assertNotIn("year_max", out)

    def test_no_age_key_returns_unchanged(self):
        before = {"year_min": 2022, "year_max": 2024}
        out = apply_age_filter(dict(before), today=date(2026, 4, 24))
        self.assertEqual(out, before)

    def test_non_int_age_is_ignored(self):
        # Defensive: parsers normally only set int, but make sure a stray
        # value doesn't blow up — and is dropped, not propagated.
        out = apply_age_filter({"age": "two"}, today=date(2026, 4, 24))
        self.assertNotIn("age", out)
        self.assertNotIn("year_min", out)

    def test_age_zero_yields_current_year(self):
        out = apply_age_filter({"age": 0}, today=date(2026, 4, 24))
        self.assertEqual(out["year_min"], 2026)

    def test_default_today_uses_real_date(self):
        out = apply_age_filter({"age": 1})
        self.assertEqual(out["year_min"], date.today().year - 1)


if __name__ == "__main__":
    unittest.main()
