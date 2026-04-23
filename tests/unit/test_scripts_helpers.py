"""Unit tests for small pure helpers inside the scripts."""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests._helpers import ROOT  # noqa: F401

import bidfax_info
import build_workbook
import price_fix
import price_refresh
import workbook_to_html


class TestBuildOutputFieldnames(unittest.TestCase):
    def test_price_inserted_after_odometer_vin_appended(self):
        src = ["Make", "Model", "Year", "Odometer", "Fuel Type", "Lot Number", "Link"]
        out = bidfax_info._build_output_fieldnames(src)
        self.assertEqual(out.index("Price"), out.index("Odometer") + 1)
        self.assertEqual(out[-1], "VIN")

    def test_no_odometer_price_at_end(self):
        out = bidfax_info._build_output_fieldnames(["Make", "Lot Number"])
        self.assertIn("Price", out)
        self.assertEqual(out[-1], "VIN")


class TestBuildOutputRow(unittest.TestCase):
    def test_sets_price_vin_and_link(self):
        row = {"Make": "HONDA", "Link": "original"}
        out = bidfax_info._build_output_row(row, "$100", "VIN1", "https://bidfax.info/new")
        self.assertEqual(out["Price"], "$100")
        self.assertEqual(out["VIN"], "VIN1")
        self.assertEqual(out["Link"], "https://bidfax.info/new")

    def test_empty_url_keeps_original_link(self):
        row = {"Make": "HONDA", "Link": "original"}
        out = bidfax_info._build_output_row(row, "$100", "VIN1", "")
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


class TestPriceRefreshPattern(unittest.TestCase):
    def test_matches_price_not_search(self):
        self.assertTrue(price_refresh.FILE_PATTERN.match("copart_price_2026_01_01.csv"))
        self.assertTrue(price_refresh.FILE_PATTERN.match("iaai_price_2026_01_01.csv"))
        self.assertIsNone(price_refresh.FILE_PATTERN.match("copart_search_2026_01_01.csv"))
        self.assertIsNone(price_refresh.FILE_PATTERN.match("readme.txt"))


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
    def test_normalise_iaai_date(self):
        out = workbook_to_html._normalize_auction_date("Thu Apr 09, 8:30am CDT")
        self.assertIn("04-09", out)
        self.assertIn("08:30", out)
        self.assertIn("CDT", out)

    def test_normalise_pm_date(self):
        out = workbook_to_html._normalize_auction_date("Thu Apr 09, 1:30pm CDT")
        self.assertIn("13:30", out)

    def test_leaves_already_normalised(self):
        s = "2026-04-09 13:30 UTC"
        self.assertEqual(workbook_to_html._normalize_auction_date(s), s)

    def test_model_key_first_word(self):
        self.assertEqual(workbook_to_html._model_key("GLE 350 4MATIC"), "GLE")
        self.assertEqual(workbook_to_html._model_key("CR-V HYBRID"),   "CR-V")
        self.assertEqual(workbook_to_html._model_key(""),              "")


if __name__ == "__main__":
    unittest.main()
