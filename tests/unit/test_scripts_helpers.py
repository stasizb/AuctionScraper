"""Unit tests for small pure helpers inside the scripts."""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests._helpers import ROOT  # noqa: F401

import bidfax_run
import build_workbook
import price_fix
import workbook_to_html


class TestBuildOutputFieldnames(unittest.TestCase):
    """Same logic that used to live in bidfax_info._build_output_fieldnames
    now lives in bidfax_run._build_output_fieldnames — Price column gets
    inserted right after Odometer, VIN column appended at the end."""

    def test_price_inserted_after_odometer_vin_appended(self):
        src = ["Make", "Model", "Year", "Odometer", "Fuel Type", "Lot Number", "Link"]
        out = bidfax_run._build_output_fieldnames(src)
        self.assertEqual(out.index("Price"), out.index("Odometer") + 1)
        self.assertEqual(out[-1], "VIN")

    def test_no_odometer_price_at_end(self):
        out = bidfax_run._build_output_fieldnames(["Make", "Lot Number"])
        self.assertIn("Price", out)
        self.assertEqual(out[-1], "VIN")


class TestBuildNewRow(unittest.TestCase):
    """`bidfax_run._build_new_row` is the consolidated equivalent of the
    old bidfax_info._build_output_row — overlays Price / VIN / bidfax URL
    onto the source-search row."""

    def test_sets_price_vin_and_link(self):
        row = {"Make": "HONDA", "Link": "original"}
        out = bidfax_run._build_new_row(row, "$100", "VIN1",
                                        "https://bidfax.info/new")
        self.assertEqual(out["Price"], "$100")
        self.assertEqual(out["VIN"],   "VIN1")
        self.assertEqual(out["Link"],  "https://bidfax.info/new")

    def test_empty_url_keeps_original_link(self):
        row = {"Make": "HONDA", "Link": "original"}
        out = bidfax_run._build_new_row(row, "$100", "VIN1", "")
        self.assertEqual(out["Link"], "original")


class TestBuildWorkbook(unittest.TestCase):
    def test_parse_last_price_plain(self):
        price, vin = build_workbook.parse_last_price("$1,234 | VIN: ABC123")
        self.assertEqual(price, "$1,234")
        self.assertEqual(vin, "ABC123")

    def test_parse_last_price_no_vin(self):
        price, vin = build_workbook.parse_last_price("In Progress")
        self.assertEqual(price, "In Progress")
        self.assertEqual(vin, "")

    def test_build_headers_from_bidcars_format(self):
        src  = ["Make", "Model", "Odometer", "Last Price", "Link"]
        out  = build_workbook._build_headers(src)
        self.assertNotIn("Last Price", out)
        self.assertIn("Price", out)
        self.assertEqual(out[-1], "VIN")

    def test_build_headers_from_bidfax_format(self):
        src = ["Make", "Odometer", "Price", "Lot Number", "VIN"]
        out = build_workbook._build_headers(src)
        self.assertEqual(out, src)


class TestFindPendingFiles(unittest.TestCase):
    def test_only_past_and_unprocessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "copart_price_2026_01_01.csv").touch()
            (d / "iaai_price_2030_01_01.csv").touch()          # future — skip
            (d / "notes.txt").touch()                           # non-csv — skip
            (d / "copart_search_2026_01_01.csv").touch()        # search — skip
            (d / "copart_price_2026_01_02.csv").touch()         # already processed
            processed = {"copart_price_2026_01_02.csv"}
            pending = build_workbook.find_pending_files(d, date(2026, 6, 1), processed)
            names = {p.name for p in pending}
            self.assertIn("copart_price_2026_01_01.csv", names)
            self.assertNotIn("iaai_price_2030_01_01.csv", names)
            self.assertNotIn("copart_price_2026_01_02.csv", names)
            self.assertNotIn("copart_search_2026_01_01.csv", names)


class TestPriceFilePattern(unittest.TestCase):
    """Pattern used by find_price_files to decide whether a CSV is a
    price file. Lives in core/csv_io.py now — the price_refresh script
    that used to re-export it has been deleted."""

    def test_matches_price_not_search(self):
        from core.csv_io import PRICE_FILE_PATTERN
        self.assertTrue(PRICE_FILE_PATTERN.match("copart_price_2026_01_01.csv"))
        self.assertTrue(PRICE_FILE_PATTERN.match("iaai_price_2026_01_01.csv"))
        self.assertIsNone(PRICE_FILE_PATTERN.match("copart_search_2026_01_01.csv"))
        self.assertIsNone(PRICE_FILE_PATTERN.match("readme.txt"))


class TestPriceFixParseLots(unittest.TestCase):
    def test_comma_separated(self):
        self.assertEqual(price_fix._parse_lots("1, 2,3"), ["1", "2", "3"])

    def test_semicolon_separated(self):
        self.assertEqual(price_fix._parse_lots("1; 2;3"), ["1", "2", "3"])

    def test_mixed(self):
        self.assertEqual(price_fix._parse_lots("1,2; 3, 4"), ["1", "2", "3", "4"])

    def test_skips_empty(self):
        self.assertEqual(price_fix._parse_lots(",, 1 ,,"), ["1"])


class TestWorkbookToHtmlDates(unittest.TestCase):
    """Thin sanity checks — full coverage lives in tests/unit/test_dates.py."""

    def test_workbook_to_html_reexports_normalizer(self):
        # Cell rendering calls workbook_to_html._normalize_auction_date, which
        # must be the same function that core.dates provides.
        import core.dates
        self.assertIs(workbook_to_html._normalize_auction_date,
                      core.dates.normalize_auction_date)

    def test_model_key_first_word(self):
        self.assertEqual(workbook_to_html._model_key("GLE 350 4MATIC"), "GLE")
        self.assertEqual(workbook_to_html._model_key("CR-V HYBRID"),   "CR-V")
        self.assertEqual(workbook_to_html._model_key(""),              "")


if __name__ == "__main__":
    unittest.main()
