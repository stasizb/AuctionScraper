"""Unit tests for clients/copart.py and scripts/copart_search.py pure helpers."""

import unittest

from tests._helpers import ROOT  # noqa: F401

from clients.copart import (
    FakeCopartClient,
    _extract_lot_number,
    build_search_payload,
    check_sale_ended_via_search,
)

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

    def test_build_lot_url_prefers_ldu(self):
        # `ldu` is the canonical URL slug Copart's API ships — should be
        # used verbatim, not re-derived from `ld`. This is what makes the
        # downstream "Sale ended" check hit the right page.
        lot = {"ln": "84082334",
               "ld":  "2024 AUDI A6 PREMIUM",
               "ldu": "salvage-2024-audi-a6-premium-al-tanner"}
        url = copart_search.build_lot_url(lot)
        self.assertEqual(
            url,
            "https://www.copart.com/lot/84082334/"
            "salvage-2024-audi-a6-premium-al-tanner",
        )

    def test_build_lot_url_falls_back_to_ld_slugify(self):
        # Legacy payloads without `ldu` still produce a usable URL.
        lot = {"ln": "12345", "ld": "Honda CR-V Hybrid"}
        url = copart_search.build_lot_url(lot)
        self.assertEqual(url, "https://www.copart.com/lot/12345/honda-cr-v-hybrid")

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


class _FakeResp:
    """Minimal stand-in for requests.Response, captures one search batch."""

    def __init__(self, *, status_code=200, content=None, raise_=None):
        self.status_code = status_code
        self._content    = content or []
        self._raise      = raise_

    def raise_for_status(self):
        if self._raise:
            raise self._raise

    def json(self):
        return {"data": {"results": {"content": self._content}}}


class _FakeSession:
    """Records POST payloads and replays a scripted list of responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.posts      = []

    def post(self, url, *, json=None, timeout=None):
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        return self._responses.pop(0)


class TestExtractLotNumber(unittest.TestCase):
    def test_canonical_slug(self):
        self.assertEqual(_extract_lot_number(
            "https://www.copart.com/lot/47179226/salvage-2025-mazda"), "47179226")

    def test_bare(self):
        self.assertEqual(_extract_lot_number("https://www.copart.com/lot/84082334"),
                         "84082334")

    def test_none_on_garbage(self):
        self.assertIsNone(_extract_lot_number("nope"))
        self.assertIsNone(_extract_lot_number(""))
        self.assertIsNone(_extract_lot_number("https://www.copart.com/about"))


class TestCheckSaleEndedViaSearch(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(check_sale_ended_via_search(_FakeSession([]), []), {})

    def test_absence_is_ended_presence_is_active(self):
        # One API call per unique lot number.
        # 47179226 → empty content → ended.
        # 84082334 → its own ln present → active.
        sess = _FakeSession([
            _FakeResp(content=[]),
            _FakeResp(content=[{"ln": 84082334, "ld": "AUDI A6"}]),
        ])
        urls = [
            "https://www.copart.com/lot/47179226/salvage-2025-mazda",
            "https://www.copart.com/lot/84082334/salvage-2024-audi",
        ]
        result = check_sale_ended_via_search(sess, urls)
        self.assertTrue(result["https://www.copart.com/lot/47179226/salvage-2025-mazda"])
        self.assertFalse(result["https://www.copart.com/lot/84082334/salvage-2024-audi"])
        self.assertEqual(len(sess.posts), 2)
        # Each payload is a single-token freeFormSearch query.
        for post in sess.posts:
            sent = post["json"]
            self.assertTrue(sent["freeFormSearch"])
            self.assertEqual(len(sent["query"]), 1)
            # The token is the lot number, no spaces.
            self.assertNotIn(" ", sent["query"][0])

    def test_url_without_lot_number_maps_to_false(self):
        # Only the lot-1 URL produces an API call (garbage URL is skipped).
        sess = _FakeSession([_FakeResp(content=[])])
        result = check_sale_ended_via_search(
            sess, ["https://www.copart.com/about", "https://www.copart.com/lot/1/x"])
        self.assertFalse(result["https://www.copart.com/about"])      # garbage → False
        self.assertTrue(result["https://www.copart.com/lot/1/x"])      # lot 1 absent → ended
        self.assertEqual(len(sess.posts), 1)

    def test_one_api_call_per_unique_lot(self):
        # 3 lots → 3 API calls. Lot 200 isn't found in its own response.
        urls = [f"https://www.copart.com/lot/{ln}/x" for ln in ("100", "200", "300")]
        sess = _FakeSession([
            _FakeResp(content=[{"ln": 100}]),  # active
            _FakeResp(content=[]),              # ended
            _FakeResp(content=[{"ln": 300}]),  # active
        ])
        result = check_sale_ended_via_search(sess, urls)
        self.assertFalse(result[urls[0]])  # 100 active
        self.assertTrue(result[urls[1]])   # 200 ended
        self.assertFalse(result[urls[2]])  # 300 active
        self.assertEqual(len(sess.posts), 3)

    def test_failure_marks_lot_as_not_ended(self):
        # If the API raises for a given lot, treat it as not-ended so we
        # don't accidentally evict a valid lot on a transient hiccup.
        class Boom(Exception):
            pass
        urls = ["https://www.copart.com/lot/42/x", "https://www.copart.com/lot/43/x"]
        sess = _FakeSession([
            _FakeResp(raise_=Boom("transient")),
            _FakeResp(content=[{"ln": 43}]),
        ])
        result = check_sale_ended_via_search(sess, urls)
        self.assertFalse(result[urls[0]])  # transient failure → not-ended
        self.assertFalse(result[urls[1]])  # genuinely active

    def test_fuzzy_extras_ignored(self):
        # If Copart's fuzzy match returns lots we didn't ask about (VIN
        # match etc.), they must NOT count as the queried lot being present.
        urls = ["https://www.copart.com/lot/200/x"]
        sess = _FakeSession([_FakeResp(content=[
            {"ln": 999},   # fuzzy noise
            {"ln": 1234},  # fuzzy noise
            # NOTE: 200 is not in the response → still ended.
        ])])
        result = check_sale_ended_via_search(sess, urls)
        self.assertTrue(result[urls[0]])

    def test_duplicate_urls_dedupe(self):
        # Two URLs pointing at the same lot only generate one API call.
        urls = [
            "https://www.copart.com/lot/55/x",
            "https://www.copart.com/lot/55/y",
        ]
        sess = _FakeSession([_FakeResp(content=[{"ln": 55}])])
        result = check_sale_ended_via_search(sess, urls)
        self.assertFalse(result[urls[0]])
        self.assertFalse(result[urls[1]])
        self.assertEqual(len(sess.posts), 1)


if __name__ == "__main__":
    unittest.main()
