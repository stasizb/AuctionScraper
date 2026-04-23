"""Integration tests for scripts/build_workbook.py — pure file I/O, no browser."""

import csv
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

import openpyxl

from tests._helpers import CSV_FIXTURES, ROOT  # noqa: F401

import build_workbook


class TestBuildWorkbook(unittest.TestCase):
    def setUp(self):
        self._tmp     = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_new_workbook_from_csv(self):
        # Copy the test price CSV (has both a confirmed and In Progress rows)
        shutil.copy(
            CSV_FIXTURES / "copart_price_with_in_progress.csv",
            self.work_dir / "copart_price_2026_01_02.csv",
        )
        workbook_path = self.work_dir / "auction_results.xlsx"

        pending = build_workbook.find_pending_files(
            self.work_dir, date(2026, 6, 1), processed=set(),
        )
        self.assertEqual(len(pending), 1)

        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        added = build_workbook.process_csv(pending[0], wb)
        self.assertEqual(added, 3)
        wb.save(workbook_path)

        # Verify: one sheet per Make
        wb = openpyxl.load_workbook(workbook_path)
        self.assertEqual(set(wb.sheetnames), {"HONDA", "AUDI"})
        honda = wb["HONDA"]
        rows = list(honda.iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(rows), 2)
        headers = [c.value for c in honda[1]]
        self.assertIn("Price", headers)
        self.assertIn("VIN",   headers)
        self.assertIn("Lot Number", headers)

    def test_processed_file_skipped(self):
        shutil.copy(
            CSV_FIXTURES / "copart_price_with_in_progress.csv",
            self.work_dir / "copart_price_2026_01_02.csv",
        )
        processed = {"copart_price_2026_01_02.csv"}
        pending = build_workbook.find_pending_files(
            self.work_dir, date(2026, 6, 1), processed,
        )
        self.assertEqual(pending, [])

    def test_today_file_skipped(self):
        # Files dated today must NOT be imported (day hasn't ended)
        today = date.today().strftime("%Y_%m_%d")
        path  = self.work_dir / f"copart_price_{today}.csv"
        shutil.copy(CSV_FIXTURES / "copart_price_with_in_progress.csv", path)
        pending = build_workbook.find_pending_files(self.work_dir, date.today(), set())
        self.assertEqual(pending, [])


if __name__ == "__main__":
    unittest.main()
