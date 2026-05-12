"""Unit tests for clients/copart.py and scripts/copart_search.py pure helpers."""

import unittest

from tests._helpers import ROOT  # noqa: F401

from clients.copart import FakeCopartClient, build_search_payload

import copart_search  # from scripts/


class TestBuildSearchPayload(unittest.TestCase):
    def test_always_sets_run_and_drive(self):
        p = build_search_payload({"make": "HONDA"})
        self.assertEqual(p["filter"]["FETI"], ["lot_condition_code:CERT-D"])

    def test_make_filter(self):
        p = build_search_payload({"make": "HONDA"})
        self.assertEqual(p["filter"]["MAKE"], ['lot_make_desc:"HONDA"'])

    def test_multiple_models(self):
        p = build_search_payload({"models": ["CR-V", "Pilot"]})
        self.assertEqual(p["filter"]["MODL"],
                         ['lot_model_desc:"CR-V"', 'lot_model_desc:"Pilot"'])

    def test_odometer(self):
        p = build_search_payload({"odometer_max": 30000})
        self.assertIn("ODM", p["filter"])
        self.assertEqual(p["filter"]["ODM"], ["odometer_reading_received:[0 TO 30000]"])

    def test_fuel_type(self):
        p = build_search_payload({"fuel_type": "HYBRID ENGINE"})
        self.assertEqual(p["filter"]["FUEL"], ['fuel_type_desc:"HYBRID ENGINE"'])

    def test_pagination(self):
        p0 = build_search_payload({}, page=0)
        p2 = build_search_payload({}, page=2)
        self.assertEqual(p0["start"], 0)
        self.assertEqual(p2["start"], 200)


class TestFakeCopartClient(unittest.TestCase):
    def test_flat_lots(self):
        c = FakeCopartClient(lots=[{"ln": "1"}, {"ln": "2"}])
        self.assertEqual(len(c.fetch_lots({"make": "HONDA"})), 2)
        self.assertEqual(c.calls, [{"make": "HONDA"}])

    def test_callable_fetch_fn(self):
        def fetch(filters):
            return [{"ln": "matched"}] if filters.get("make") == "AUDI" else []
        c = FakeCopartClient(fetch_fn=fetch)
        self.assertEqual(len(c.fetch_lots({"make": "AUDI"})), 1)
        self.assertEqual(len(c.fetch_lots({"make": "BMW"})), 0)


class TestCopartSearchHelpers(unittest.TestCase):
    def test_parse_filter_row_basic(self):
        f = copart_search.parse_filter_row(
            "Make: Honda, Model: CR-V, Year min: 2023, Odometer max: 30000"
        )
        self.assertEqual(f["make"], "HONDA")
        self.assertEqual(f["models"], ["CR-V"])
        self.assertEqual(f["year_min"], 2023)
        self.assertEqual(f["odometer_max"], 30000)

    def test_parse_filter_row_multi_model(self):
        f = copart_search.parse_filter_row("Make: Lincoln, Model: Corsair;Nautilus")
        self.assertEqual(f["models"], ["CORSAIR", "NAUTILUS"])

    def test_parse_filter_row_age_sets_year_min(self):
        from datetime import date
        f = copart_search.parse_filter_row("Make: Honda, Model: CR-V, Age: 2")
        self.assertEqual(f["year_min"], date.today().year - 2)
        self.assertNotIn("year_max", f)
        self.assertNotIn("age", f)

    def test_parse_filter_row_age_overrides_explicit_year_bounds(self):
        from datetime import date
        f = copart_search.parse_filter_row(
            "Make: Honda, Model: CR-V, Year min: 2010, Year max: 2015, Age: 3"
        )
        self.assertEqual(f["year_min"], date.today().year - 3)
        self.assertNotIn("year_max", f)

    def test_equipment_ok_missing(self):
        self.assertTrue(copart_search.equipment_ok({}, None))

    def test_equipment_ok_title_match(self):
        lot = {"ld": "Honda CR-V TOURING 2024"}
        self.assertTrue(copart_search.equipment_ok(lot, "Touring"))

    def test_equipment_ok_no_match(self):
        lot = {"ld": "Honda CR-V EX 2024"}
        self.assertFalse(copart_search.equipment_ok(lot, "Touring"))

    def test_equipment_ok_finds_trim_in_other_fields(self):
        # `ld` is a short description without the trim — the trim "4MATIC"
        # only shows up in a different API field. The filter must still pass.
        lot = {"ld": "Mercedes-Benz GLB 250", "lm": "GLB 250 4MATIC"}
        self.assertTrue(copart_search.equipment_ok(lot, "4MATIC"))

    def test_equipment_ok_ignores_non_string_fields(self):
        # Numeric / non-string lot fields must not crash the scan.
        lot = {"ld": "Honda CR-V TOURING 2024", "lcy": 2024, "ad": 1700000000000}
        self.assertTrue(copart_search.equipment_ok(lot, "Touring"))

    def test_equipment_ok_multiword_interleaved(self):
        # "Premium 45" must match a title where another token sits between
        # the two words ("Premium Plus 45") — each word is checked
        # independently, not as a concatenated substring.
        lot = {"ld": "2024 AUDI Q5 PREMIUM PLUS 45  "}
        self.assertTrue(copart_search.equipment_ok(lot, "Premium 45"))

    def test_equipment_ok_multiword_missing_one(self):
        lot = {"ld": "2024 AUDI Q5 PREMIUM PLUS"}
        self.assertFalse(copart_search.equipment_ok(lot, "Premium 45"))

    def test_lot_to_row(self):
        lot = {
            "ln":  "12345",
            "mkn": "HONDA",
            "lm":  "CR-V HYBRID",
            "lcy": 2024,
            "orr": "15000",
            "ftd": "HYBRID ENGINE",
            "ad":  1700000000000,
            "yn":  "CO - DENVER",
            "dd":  "REAR END",
            "ld":  "Honda CR-V Hybrid",
        }
        row = copart_search.lot_to_row(lot, {"make": "HONDA"})
        self.assertEqual(row["Lot Number"], "12345")
        self.assertEqual(row["Make"],       "HONDA")
        self.assertEqual(row["Model"],      "CR-V HYBRID")
        self.assertEqual(row["Year"],       2024)
        self.assertTrue(row["Link"].startswith("https://www.copart.com/lot/12345/"))
        self.assertIn("UTC", row["Auction Date"])


if __name__ == "__main__":
    unittest.main()
