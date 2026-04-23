"""Unit tests for clients/iaai.py pure helpers."""

import unittest

from tests._helpers import ROOT  # noqa: F401

from clients.iaai import (
    FakeIAAIClient, OUTPUT_FIELDS,
    _parse_scraped_row,
    apply_equipment_postfilter, equipment_matches,
    parse_filter_row, read_filters_csv,
)


class TestEquipmentMatches(unittest.TestCase):
    def test_empty_equipment_passes(self):
        self.assertTrue(equipment_matches("anything", ""))

    def test_all_words_present_any_order(self):
        self.assertTrue(equipment_matches(
            "2022 AUDI Q5 PREMIUM PLUS 45 TFSI S LINE QUATTRO", "Premium 45"))
        self.assertTrue(equipment_matches(
            "2022 AUDI Q5 45 PREMIUM PLUS TFSI S LINE QUATTRO", "Premium 45"))

    def test_missing_word_fails(self):
        self.assertFalse(equipment_matches(
            "2022 AUDI Q5 PREMIUM PLUS TFSI S LINE QUATTRO", "Premium 45"))

    def test_case_insensitive(self):
        self.assertTrue(equipment_matches("audi q5 PREMIUM 45", "premium 45"))


class TestParseFilterRow(unittest.TestCase):
    def test_basic(self):
        f = parse_filter_row("Make: Honda, Model: CR-V, Year min: 2023")
        self.assertEqual(f["make"], "HONDA")
        self.assertEqual(f["models"], ["CR-V"])
        self.assertEqual(f["year_min"], 2023)

    def test_multi_model_semicolon(self):
        f = parse_filter_row("Make: Lincoln, Model: Corsair;Nautilus")
        self.assertEqual(f["models"], ["CORSAIR", "NAUTILUS"])

    def test_reassemble_comma_in_value(self):
        # value without colon after a comma should be reattached
        f = parse_filter_row("Make: Mercedes-Benz, Model: GLE, Equipment: 4MATIC Suv")
        self.assertEqual(f["make"], "MERCEDES-BENZ")
        self.assertEqual(f["equipment"], "4MATIC Suv")

    def test_unknown_keys_ignored(self):
        f = parse_filter_row("Make: HONDA, Unknown: junk")
        self.assertEqual(set(f.keys()), {"make"})


class TestReadFiltersCsv(unittest.TestCase):
    def test_skips_blank_and_comments(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.csv"
            p.write_text(
                "# comment\n"
                "\n"
                "Make: HONDA, Model: CR-V\n"
                "Make: AUDI, Model: Q5\n"
            )
            rows = read_filters_csv(str(p))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["make"], "HONDA")
            self.assertEqual(rows[1]["make"], "AUDI")


class TestEquipmentPostfilter(unittest.TestCase):
    def test_drops_non_matching_rows(self):
        rows = [
            {"_full_title": "2024 AUDI Q5 PREMIUM 45"},
            {"_full_title": "2024 AUDI Q5 PREMIUM PLUS"},
        ]
        kept = apply_equipment_postfilter(rows, "Premium 45")
        self.assertEqual(len(kept), 1)

    def test_empty_equipment_keeps_all(self):
        rows = [{"_full_title": "x"}, {"_full_title": "y"}]
        self.assertEqual(len(apply_equipment_postfilter(rows, "")), 2)


class TestParseScrapedRow(unittest.TestCase):
    """_parse_scraped_row is where the IAAI auction date gets normalized."""

    def test_auction_date_converted_to_utc(self):
        raw = {
            "Make": "HONDA", "Model": "CR-V", "Year": "2024",
            "Lot Number": "44444444", "Link": "https://iaai/x",
            "Auction Date": "Tue Apr 21, 8:30am CDT",
        }
        record = _parse_scraped_row(raw)
        self.assertIsNotNone(record)
        # 8:30 CDT = 13:30 UTC (DST, UTC-5)
        self.assertEqual(record["Auction Date"][-4:], " UTC")
        self.assertIn("13:30", record["Auction Date"])

    def test_already_canonical_date_untouched(self):
        raw = {"Link": "https://iaai/x",
               "Auction Date": "2026-04-21 13:30 UTC"}
        record = _parse_scraped_row(raw)
        self.assertEqual(record["Auction Date"], "2026-04-21 13:30 UTC")

    def test_empty_date_left_empty(self):
        raw = {"Link": "https://iaai/x", "Auction Date": ""}
        record = _parse_scraped_row(raw)
        self.assertEqual(record["Auction Date"], "")


class TestFakeIAAIClient(unittest.TestCase):
    def test_flat_rows(self):
        c = FakeIAAIClient(rows=[{"Lot Number": "1"}, {"Lot Number": "2"}])
        self.assertEqual(len(c.scrape_with_filters({"make": "HONDA"})), 2)
        self.assertEqual(c.calls, [{"make": "HONDA"}])

    def test_callable_scrape_fn(self):
        c = FakeIAAIClient(scrape_fn=lambda f: [{"x": f.get("make")}])
        self.assertEqual(c.scrape_with_filters({"make": "AUDI"}), [{"x": "AUDI"}])

    def test_output_fields_stable(self):
        # Guard: workbook builder depends on this column order
        self.assertEqual(OUTPUT_FIELDS[:6],
                         ["Make", "Model", "Year", "Odometer", "Fuel Type", "Lot Number"])


if __name__ == "__main__":
    unittest.main()
