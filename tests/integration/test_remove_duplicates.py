"""Integration tests for scripts/remove_duplicates.py — pure file I/O."""

import csv
import shutil
import tempfile
import unittest
from pathlib import Path

from tests._helpers import CSV_FIXTURES, ROOT  # noqa: F401

import remove_duplicates


def _write_csv(path: Path, lots: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Make", "Model", "Year", "Odometer", "Fuel Type",
                    "Lot Number", "Link", "Auction Date", "Location",
                    "Primary Damage"])
        for lot in lots:
            w.writerow(["MAKE", "MODEL", "2024", "10000", "Gas",
                        lot, "url", "date", "loc", "dmg"])


class TestRemoveDuplicates(unittest.TestCase):
    def setUp(self):
        self._tmp     = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_removes_lots_from_src_that_exist_in_dest(self):
        """src is rewritten in place; rows duplicating dest are dropped."""
        src  = self.work_dir / "src.csv"
        dest = self.work_dir / "dest.csv"
        # src (yesterday) has 4 lots; dest (today) has 2 of them + one unique
        _write_csv(src,  ["111", "222", "333", "444"])
        _write_csv(dest, ["222", "333", "555"])

        removed = remove_duplicates.remove_duplicate_lots(src, dest)
        self.assertEqual(removed, 2)

        # src now only has lots NOT in dest
        with src.open() as fh:
            src_lots = [r["Lot Number"] for r in csv.DictReader(fh)]
        self.assertEqual(src_lots, ["111", "444"])

        # dest is untouched
        with dest.open() as fh:
            dest_lots = [r["Lot Number"] for r in csv.DictReader(fh)]
        self.assertEqual(dest_lots, ["222", "333", "555"])

    def test_no_duplicates_returns_zero_and_leaves_src_untouched(self):
        src  = self.work_dir / "src.csv"
        dest = self.work_dir / "dest.csv"
        _write_csv(src,  ["111", "222"])
        _write_csv(dest, ["333", "444"])

        removed = remove_duplicates.remove_duplicate_lots(src, dest)
        self.assertEqual(removed, 0)
        with src.open() as fh:
            src_lots = [r["Lot Number"] for r in csv.DictReader(fh)]
        self.assertEqual(src_lots, ["111", "222"])


if __name__ == "__main__":
    unittest.main()
