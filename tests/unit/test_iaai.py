"""Unit tests for clients/iaai.py pure helpers."""

import unittest

from tests._helpers import ROOT  # noqa: F401

from clients.iaai import (
    AUCTION_DATE_COL,
    DEFAULT_TAB_CONCURRENCY,
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

    def test_age_sets_year_min(self):
        from datetime import date
        f = parse_filter_row("Make: Honda, Model: CR-V, Age: 2")
        self.assertEqual(f["year_min"], date.today().year - 2)
        self.assertNotIn("year_max", f)
        self.assertNotIn("age", f)

    def test_age_overrides_explicit_year_bounds(self):
        from datetime import date
        f = parse_filter_row(
            "Make: Honda, Model: CR-V, Year min: 2010, Year max: 2015, Age: 3"
        )
        self.assertEqual(f["year_min"], date.today().year - 3)
        self.assertNotIn("year_max", f)


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

    def test_scrape_many_iterates_all_filter_rows(self):
        c = FakeIAAIClient(scrape_fn=lambda f: [{"make": f.get("make")}])
        out = c.scrape_many([{"make": "HONDA"}, {"make": "AUDI"}, {"make": "BMW"}])
        self.assertEqual([r["make"] for r in out], ["HONDA", "AUDI", "BMW"])
        self.assertEqual(len(c.calls), 3)

    def test_scrape_many_empty_list(self):
        c = FakeIAAIClient(rows=[{"x": 1}])
        self.assertEqual(c.scrape_many([]), [])
        self.assertEqual(c.calls, [])

    def test_scrape_with_filters_delegates_to_scrape_many_in_fake(self):
        # Both entry points must produce the same rows — tests shouldn't care
        # which one they use, and scripts can call either.
        c = FakeIAAIClient(rows=[{"Lot Number": "x"}])
        self.assertEqual(c.scrape_with_filters({"make": "HONDA"}),
                         c.scrape_many([{"make": "HONDA"}]))


class TestConcurrencyConstantStillExported(unittest.TestCase):
    """The DEFAULT_TAB_CONCURRENCY re-export from clients.iaai survives the
    Browser → Session refactor. The new SessionIAAIClient doesn't drive
    parallel tabs itself (the daily run is a single POST per filter row),
    but other importers still expect the constant on this module."""

    def test_default_constant_is_positive(self):
        self.assertIsInstance(DEFAULT_TAB_CONCURRENCY, int)
        self.assertGreaterEqual(DEFAULT_TAB_CONCURRENCY, 1)


class TestBuildSearchPayload(unittest.TestCase):
    """Pure payload-builder for the IAAI /Search API. Validates the
    canonical Facet/LongRange shapes we discovered via live probing."""

    def _searches(self, payload):
        return payload["Searches"]

    def test_baseline_always_applied(self):
        from clients.iaai import build_search_payload
        # Even with no filters, the baseline 4 Search blocks must be present:
        # Default=True, AuctionDate=AuctionToday, ODOValue range, Run & Drive.
        p = build_search_payload({})
        groups = [s["Facets"][0]["Group"]
                  for s in self._searches(p) if s.get("Facets")]
        self.assertIn("Default",     groups)
        self.assertIn("AuctionDate", groups)
        self.assertIn("StartsDesc",  groups)
        # ODOValue is a LongRange, not a Facet — check it separately.
        long_ranges = [lr for s in self._searches(p)
                       for lr in (s.get("LongRanges") or [])]
        self.assertTrue(any(lr.get("Name") == "ODOValue" for lr in long_ranges))

    def test_make_becomes_facet_model_does_not(self):
        # Make IS sent as a Facet; Model is NOT (it's post-filtered in
        # Python — see build_search_payload's docstring for why). This
        # guards against accidentally re-adding the strict-Model facet
        # that was empirically observed to miss sub-trims (e.g. the
        # API's Model="CR-V HYBRID" doesn't include AWD SPORT TOURING).
        from clients.iaai import build_search_payload
        p = build_search_payload({"make": "Honda", "models": ["CR-V", "Pilot"]})
        make = next((s for s in self._searches(p)
                     if s.get("Facets") and s["Facets"][0]["Group"] == "Make"), None)
        self.assertIsNotNone(make)
        self.assertEqual(make["Facets"][0]["Value"], "HONDA")
        # No Search block should carry a Model facet.
        for s in self._searches(p):
            for f in (s.get("Facets") or []):
                self.assertNotEqual(f.get("Group"), "Model",
                    "Model must be filtered in Python, not as an API facet")

    def test_year_range_expands_to_per_year_facets(self):
        from clients.iaai import build_search_payload
        p = build_search_payload({"year_min": 2023, "year_max": 2025})
        year = next((s for s in self._searches(p)
                     if s.get("Facets")
                     and s["Facets"][0]["Group"] == "Year"), None)
        self.assertIsNotNone(year)
        self.assertEqual([f["Value"] for f in year["Facets"]],
                         ["2023", "2024", "2025"])

    def test_fuel_type_canonicalized(self):
        from clients.iaai import build_search_payload
        # CSV says "Gas" → API expects "Gasoline".
        p = build_search_payload({"fuel_type": "Gas"})
        fuel = next((s for s in self._searches(p)
                     if s.get("Facets")
                     and s["Facets"][0]["Group"] == "FuelTypeDesc"), None)
        self.assertIsNotNone(fuel)
        self.assertEqual(fuel["Facets"][0]["Value"], "Gasoline")
        # CSV "Hybrid Engine" also maps to "Hybrid".
        p = build_search_payload({"fuel_type": "Hybrid Engine"})
        fuel = next((s for s in self._searches(p)
                     if s.get("Facets")
                     and s["Facets"][0]["Group"] == "FuelTypeDesc"), None)
        self.assertEqual(fuel["Facets"][0]["Value"], "Hybrid")

    def test_odometer_max_overrides_baseline_default(self):
        from clients.iaai import build_search_payload
        p = build_search_payload({"odometer_max": 50000})
        lr = next((lr for s in self._searches(p)
                   for lr in (s.get("LongRanges") or [])
                   if lr.get("Name") == "ODOValue"), None)
        self.assertEqual(lr["To"], 50000)

    def test_no_filters_omits_optional_blocks(self):
        # Without make/models/year/fuel, only the 4 baseline blocks are sent.
        from clients.iaai import build_search_payload
        p = build_search_payload({})
        self.assertEqual(len(p["Searches"]), 4)

    def test_top_level_envelope(self):
        from clients.iaai import build_search_payload, PAGE_SIZE
        p = build_search_payload({})
        self.assertEqual(p["PageSize"],   PAGE_SIZE)
        self.assertEqual(p["CurrentPage"], 1)
        # SaleStatus / BidStatus filters intentionally empty — see the
        # comment in build_search_payload for why.
        self.assertEqual(p["SaleStatusFilters"], [])
        self.assertEqual(p["BidStatusFilters"],  [])


class TestApplyModelPostfilter(unittest.TestCase):
    """`apply_model_postfilter` keeps rows whose `_full_title` contains
    every token of any listed model. This replaces the IAAI Model facet
    (which only matches leaf sub-trims, not the user's parent name)."""

    def test_no_models_is_noop(self):
        from clients.iaai import apply_model_postfilter
        rows = [{"_full_title": "2025 HONDA HR-V"},
                {"_full_title": "2024 TOYOTA RAV4"}]
        self.assertEqual(apply_model_postfilter(rows, []),  rows)

    def test_single_model_substring_match(self):
        from clients.iaai import apply_model_postfilter
        rows = [
            {"_full_title": "2025 HONDA CR-V HYBRID AWD SPORT TOURING"},
            {"_full_title": "2025 HONDA HR-V AWD LX"},
            {"_full_title": "2024 HONDA CIVIC HYBRID SPORT"},
        ]
        # "CR-V HYBRID" keeps the Touring CR-V and the Civic Hybrid is
        # dropped (missing "CR-V"), HR-V also dropped (missing "HYBRID").
        kept = apply_model_postfilter(rows, ["CR-V HYBRID"])
        self.assertEqual([r["_full_title"] for r in kept],
                         ["2025 HONDA CR-V HYBRID AWD SPORT TOURING"])

    def test_multiple_models_any_of(self):
        # CSV "Model: GLE 350; GLB 250" → keep any row matching GLE+350
        # OR GLB+250 (in any order).
        from clients.iaai import apply_model_postfilter
        rows = [
            {"_full_title": "2024 MERCEDES-BENZ GLE 350 4MATIC"},
            {"_full_title": "2023 MERCEDES-BENZ GLB 250 4MATIC"},
            {"_full_title": "2024 MERCEDES-BENZ GLC 300"},
        ]
        kept = apply_model_postfilter(rows, ["GLE 350", "GLB 250"])
        self.assertEqual({r["_full_title"] for r in kept},
                         {"2024 MERCEDES-BENZ GLE 350 4MATIC",
                          "2023 MERCEDES-BENZ GLB 250 4MATIC"})

    def test_case_insensitive(self):
        from clients.iaai import apply_model_postfilter
        rows = [{"_full_title": "2025 mazda cx-5 turbo signature"}]
        kept = apply_model_postfilter(rows, ["CX-5"])
        self.assertEqual(len(kept), 1)

    def test_token_order_insensitive(self):
        # "Premium Plus 45" should match a title with those words in any
        # order — same semantics as equipment_matches.
        from clients.iaai import apply_model_postfilter
        rows = [{"_full_title": "2024 AUDI Q5 PREMIUM PLUS 45 TFSI"},
                {"_full_title": "2024 AUDI Q5 45 PREMIUM PLUS TFSI"},
                {"_full_title": "2024 AUDI Q5 PREMIUM 40 TFSI"}]
        kept = apply_model_postfilter(rows, ["Premium Plus 45"])
        self.assertEqual({r["_full_title"] for r in kept},
                         {"2024 AUDI Q5 PREMIUM PLUS 45 TFSI",
                          "2024 AUDI Q5 45 PREMIUM PLUS TFSI"})

    def test_missing_title_drops_row(self):
        from clients.iaai import apply_model_postfilter
        rows = [{"_full_title": ""}, {}]
        self.assertEqual(apply_model_postfilter(rows, ["CR-V"]), [])


class TestParseSearchHtml(unittest.TestCase):
    """`parse_search_html` extracts vehicle rows from server-rendered
    /Search HTML. Empty-result responses (no real rows) yield []."""

    _ROW = """
    <div class="table-row table-row-border">
      <div class="table-cell--heading">
        <a href="/VehicleDetail/123">2024 HONDA CR-V EX</a>
      </div>
      <span title="Stock #">98765432</span>
      <span title="Primary Damage">Front End</span>
      <span title="Odometer">12,345 mi</span>
      <span title="Fuel Type">Gasoline</span>
      <span title="ACV: $30,000 USD">$30,000 USD</span>
      <div class="data-list--data">
        <a aria-label="Branch Name">Chicago South (Illinois)</a>
      </div>
      <span class="data-list__value--action">Tue May 19, 8:30am CDT</span>
    </div>
    """

    def test_parses_one_row_into_canonical_dict(self):
        from clients.iaai import parse_search_html
        rows = parse_search_html(f"<html><body>{self._ROW}</body></html>")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["Lot Number"],     "98765432")
        self.assertEqual(r["Primary Damage"], "Front End")
        self.assertEqual(r["Year"],           "2024")
        self.assertEqual(r["Make"],           "HONDA")
        self.assertIn("CR-V",                 r["Model"])
        self.assertEqual(r["Location"],       "Chicago South (Illinois)")
        self.assertIn("/VehicleDetail/123",   r["Link"])
        # Auction date got canonicalised via normalize_auction_date.
        self.assertIn("UTC", r[AUCTION_DATE_COL])

    def test_empty_result_returns_empty_list(self):
        # IAAI's empty-result placeholder is a .table-row.table-row-border
        # WITHOUT a heading-link href — `_parse_scraped_row` drops rows
        # missing a Link, so the empty case naturally returns [].
        from clients.iaai import parse_search_html
        empty_placeholder = """
        <div class="table-row table-row-border">
          <div class="table-cell--heading">
            <!-- no <a href> here -->
            <span>No results</span>
          </div>
        </div>
        """
        rows = parse_search_html(f"<html><body>{empty_placeholder}</body></html>")
        self.assertEqual(rows, [])

    def test_no_rows_at_all_returns_empty_list(self):
        from clients.iaai import parse_search_html
        self.assertEqual(parse_search_html("<html><body></body></html>"), [])


if __name__ == "__main__":
    unittest.main()
