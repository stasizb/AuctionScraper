"""Integration tests for search scripts (copart_search + iaai_search) with Fake clients."""

import csv
import tempfile
import unittest
from pathlib import Path

from tests._helpers import ROOT  # noqa: F401

import copart_search
import iaai_search
from clients.copart import FakeCopartClient
from clients.iaai   import FakeIAAIClient


class TestCopartSearch(unittest.TestCase):
    def test_process_filters_with_fake_client(self):
        fake = FakeCopartClient(lots=[
            {"ln": "111", "mkn": "HONDA", "lm": "CR-V HYBRID", "lcy": 2024,
             "orr": "15000", "ftd": "HYBRID", "ad": 1700000000000,
             "yn": "CO - DENVER", "dd": "REAR END", "ld": "Honda CR-V Hybrid Touring"},
            {"ln": "222", "mkn": "HONDA", "lm": "CR-V", "lcy": 2023,
             "orr": "20000", "ftd": "GAS", "ad": 1700001000000,
             "yn": "TX - DALLAS", "dd": "FRONT", "ld": "Honda CR-V EX"},
        ])
        rows = copart_search.process_filters(
            {"make": "HONDA", "models": ["CR-V"]}, fake,
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["Lot Number"], "111")

    def test_equipment_post_filter_applied(self):
        fake = FakeCopartClient(lots=[
            {"ln": "111", "mkn": "HONDA", "ld": "Honda CR-V Touring"},
            {"ln": "222", "mkn": "HONDA", "ld": "Honda CR-V EX"},
        ])
        rows = copart_search.process_filters(
            {"make": "HONDA", "equipment": "Touring"}, fake,
        )
        lots = [r["Lot Number"] for r in rows]
        self.assertEqual(lots, ["111"])


class TestIaaiSearch(unittest.TestCase):
    def test_process_writes_csv_with_fake_client(self):
        fake_rows = [
            {"Make": "HONDA", "Model": "CR-V HYBRID SPORT TOURING", "Year": "2024",
             "Odometer": "12000", "Fuel Type": "Hybrid", "Lot Number": "44444444",
             "Link": "https://iaai/lot/1", "Auction Date": "Fri Jan 02, 11am CST",
             "Location": "Carson", "Primary Damage": "Front End",
             "ACV": "$30,000 USD", "_full_title": "2024 HONDA CR-V HYBRID SPORT TOURING"},
        ]
        fake = FakeIAAIClient(rows=fake_rows)
        with tempfile.TemporaryDirectory() as tmp:
            inp = Path(tmp) / "filters.csv"
            out = Path(tmp) / "out.csv"
            inp.write_text("Make: HONDA, Model: CR-V\n", encoding="utf-8")

            iaai_search.process(str(inp), str(out), client=fake)

            self.assertEqual(len(fake.calls), 1)
            with out.open() as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Lot Number"], "44444444")
            # The private _full_title should not leak into the output CSV
            self.assertNotIn("_full_title", rows[0])

    def test_process_iterates_over_all_filters(self):
        fake = FakeIAAIClient(scrape_fn=lambda f: [
            {"Make": f.get("make"), "Model": "X", "Lot Number": f.get("make"),
             "Link": f"https://iaai/{f.get('make')}", "Year": "2024", "Odometer": "10",
             "Fuel Type": "", "Auction Date": "", "Location": "", "Primary Damage": "",
             "ACV": ""},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            inp = Path(tmp) / "f.csv"
            out = Path(tmp) / "o.csv"
            inp.write_text("Make: HONDA, Model: CR-V\nMake: AUDI, Model: Q5\n")

            iaai_search.process(str(inp), str(out), client=fake)

            self.assertEqual(len(fake.calls), 2)
            with out.open() as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual({r["Make"] for r in rows}, {"HONDA", "AUDI"})


if __name__ == "__main__":
    unittest.main()
