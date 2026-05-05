"""Unit tests for run_daily.py's summary-tracking helpers."""

import io
import sys
import unittest
from contextlib import redirect_stdout

from tests._helpers import ROOT

sys.path.insert(0, str(ROOT))

import run_daily


class TestSummary(unittest.TestCase):
    def setUp(self):
        run_daily._step_results.clear()
        run_daily._step_timings.clear()
        run_daily._car_counts.clear()

    def tearDown(self):
        run_daily._step_results.clear()
        run_daily._step_timings.clear()
        run_daily._car_counts.clear()

    def test_skip_records_and_prints(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            run_daily.skip("5. Something", "no input file")
        self.assertIn("[SKIP]", buf.getvalue())
        self.assertEqual(run_daily._step_results,
                         [("5. Something", "skipped", "no input file")])

    def test_print_summary_formats_all_statuses(self):
        run_daily._record("1. Search",    "ok")
        run_daily._record("2. Dedup",     "skipped", "no input file")
        run_daily._record("3. Workbook",  "fail",    "exit 1")
        buf = io.StringIO()
        with redirect_stdout(buf):
            run_daily._print_summary()
        out = buf.getvalue()
        self.assertIn("1. Search",   out)
        self.assertIn("OK",          out)
        self.assertIn("SKIPPED",     out)
        self.assertIn("no input file", out)
        self.assertIn("FAIL",        out)
        self.assertIn("exit 1",      out)
        self.assertIn("1 ok",        out)
        self.assertIn("1 skipped",   out)
        self.assertIn("1 failed",    out)

    def test_print_summary_empty_noop(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            run_daily._print_summary()
        self.assertEqual(buf.getvalue(), "")


class TestFormatDuration(unittest.TestCase):
    """`_format_duration` picks the most natural unit for any seconds value."""

    def test_under_one_minute_shows_one_decimal_seconds(self):
        self.assertEqual(run_daily._format_duration(0.0),  "0.0s")
        self.assertEqual(run_daily._format_duration(12.3), "12.3s")
        self.assertEqual(run_daily._format_duration(59.9), "59.9s")

    def test_minutes_format_with_zero_padded_seconds(self):
        self.assertEqual(run_daily._format_duration(60),    "1m 00s")
        self.assertEqual(run_daily._format_duration(125.4), "2m 05s")
        self.assertEqual(run_daily._format_duration(3599),  "59m 59s")

    def test_hours_format_with_zero_padded_minutes(self):
        self.assertEqual(run_daily._format_duration(3600), "1h 00m")
        self.assertEqual(run_daily._format_duration(3725), "1h 02m")

    def test_negative_returns_em_dash(self):
        self.assertEqual(run_daily._format_duration(-1.0), "—")


class TestTimingSection(unittest.TestCase):
    """The [TIMING] block of _print_summary surfaces step durations and
    per-car throughput for the steps the user wants to track."""

    def setUp(self):
        run_daily._step_results.clear()
        run_daily._step_timings.clear()
        run_daily._car_counts.clear()

    def tearDown(self):
        run_daily._step_results.clear()
        run_daily._step_timings.clear()
        run_daily._car_counts.clear()

    def test_timing_block_renders_per_section(self):
        # At least one recorded result is needed for _print_summary to render.
        run_daily._record(run_daily.STEP_COPART_SEARCH,  "ok")
        run_daily._record(run_daily.STEP_IAAI_SEARCH,    "ok")
        run_daily._record(run_daily.STEP_BIDFAX_COPART,  "ok")
        run_daily._record(run_daily.STEP_BIDFAX_IAAI,    "ok")
        run_daily._record(run_daily.STEP_PRICE_REFRESH,  "ok")

        run_daily._step_timings.update({
            run_daily.STEP_COPART_SEARCH:  60.0,
            run_daily.STEP_IAAI_SEARCH:   120.0,
            run_daily.STEP_BIDFAX_COPART:  90.0,
            run_daily.STEP_BIDFAX_IAAI:    60.0,
            run_daily.STEP_PRICE_REFRESH:  30.0,
        })
        run_daily._car_counts.update({
            run_daily.STEP_COPART_SEARCH:   4,
            run_daily.STEP_IAAI_SEARCH:     8,
            run_daily.STEP_BIDFAX_COPART:  10,
            run_daily.STEP_BIDFAX_IAAI:     8,
            run_daily.STEP_PRICE_REFRESH:   6,
        })

        buf = io.StringIO()
        with redirect_stdout(buf):
            run_daily._print_summary()
        out = buf.getvalue()

        self.assertIn("[TIMING]",     out)
        self.assertIn("Total time",   out)
        self.assertIn("New cars   : 12", out)        # 4 + 8 = today's total

        # Each section appears with its time, car count, and per-car rate
        self.assertIn("Copart search", out)
        self.assertIn("4 cars",        out)
        self.assertIn("IAAI search",   out)
        self.assertIn("8 cars",        out)
        # Bidfax aggregates the 3 sub-steps: 90+60+30 = 180s, 10+8+6 = 24 lots
        self.assertIn("Bidfax",        out)
        self.assertIn("24 cars",       out)
        # 180s / 24 = 7.5s per car
        self.assertIn("7.5s per car",  out)

    def test_timing_block_skipped_when_no_timings(self):
        # Status recorded but no timings (e.g. all steps skipped) — the
        # [TIMING] header should NOT appear; nothing meaningful to print.
        run_daily._record("X", "skipped", "nothing")
        buf = io.StringIO()
        with redirect_stdout(buf):
            run_daily._print_summary()
        self.assertNotIn("[TIMING]", buf.getvalue())

    def test_section_with_zero_cars_shows_em_dash_per_car(self):
        run_daily._record(run_daily.STEP_COPART_SEARCH, "ok")
        run_daily._step_timings[run_daily.STEP_COPART_SEARCH] = 5.0
        run_daily._car_counts[run_daily.STEP_COPART_SEARCH]   = 0

        buf = io.StringIO()
        with redirect_stdout(buf):
            run_daily._print_summary()
        out = buf.getvalue()
        self.assertIn("Copart search", out)
        # 0 cars → can't compute per-car, must not crash and must show em-dash
        self.assertIn("—", out)


if __name__ == "__main__":
    unittest.main()
