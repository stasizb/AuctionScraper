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

    def tearDown(self):
        run_daily._step_results.clear()

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


if __name__ == "__main__":
    unittest.main()
