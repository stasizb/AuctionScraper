"""Unit tests for pure helpers in clients/bidfax.py."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from tests._helpers import ROOT  # noqa: F401  (forces sys.path setup)

# Capture asyncio.sleep BEFORE anyone monkey-patches it — tests below swap
# bidfax's asyncio.sleep for a no-op, and the no-op needs the real sleep to
# actually yield (otherwise it recurses into the patch and blows the stack).
_REAL_ASYNCIO_SLEEP = asyncio.sleep

from clients.bidfax import (
    FakeBidfaxClient, IN_PROGRESS,
    extract_grid_result, load_cache, save_cache, url_make_matches,
)


class TestUrlMakeMatches(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(url_make_matches("HONDA", "https://bidfax.info/honda/cr-v/foo.html"))

    def test_hyphenated_make(self):
        self.assertTrue(url_make_matches("MERCEDES-BENZ", "https://bidfax.info/mercedes-benz/gle/foo.html"))

    def test_space_in_make(self):
        # spaces in CSV make get normalised to hyphens
        self.assertTrue(url_make_matches("MERCEDES BENZ", "https://bidfax.info/mercedes-benz/gle/foo.html"))

    def test_wrong_make(self):
        self.assertFalse(url_make_matches("HONDA", "https://bidfax.info/toyota/rav4/foo.html"))

    def test_empty_url(self):
        self.assertFalse(url_make_matches("HONDA", ""))


class TestExtractGridResult(unittest.TestCase):
    def test_no_grid(self):
        self.assertIsNone(extract_grid_result("<html><body>nothing</body></html>"))

    def test_final_price(self):
        html = """
        <div id='grid'>
          <span class='prices'>20100</span>
          <a href='https://bidfax.info/audi/q5/36449577-audi-q5-premium-45-2023-blue-vin-wa1gaafy6p2182147.html'>x</a>
        </div>
        """
        price, vin, url = extract_grid_result(html)
        self.assertEqual(price, "$20,100")
        self.assertEqual(vin, "WA1GAAFY6P2182147")
        self.assertIn("bidfax.info", url)

    def test_in_progress(self):
        html = """
        <div id='grid'>
          <a href='https://bidfax.info/honda/cr-v/1234-honda-cr-v-vin-7fars6h97re076809.html'>x</a>
        </div>
        """
        price, vin, _ = extract_grid_result(html)
        self.assertEqual(price, IN_PROGRESS)
        self.assertEqual(vin, "7FARS6H97RE076809")

    def test_grid_without_result_link(self):
        # A grid but no URL matching the result-URL shape → no match
        html = "<div id='grid'><a href='https://other.com/foo'>x</a></div>"
        self.assertIsNone(extract_grid_result(html))


class TestCache(unittest.TestCase):
    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            data = {"12345": ("$100", "VIN1", "https://bidfax.info/x/y/z.html")}
            save_cache(path, data)
            loaded = load_cache(path)
            self.assertEqual(loaded, data)

    def test_load_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_cache(Path(tmp) / "nope.json"), {})

    def test_load_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("not valid json {{{")
            self.assertEqual(load_cache(path), {})

    def test_save_serialises_tuples_as_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            save_cache(path, {"k": ("a", "b", "c")})
            raw = json.loads(path.read_text())
            self.assertEqual(raw["k"], ["a", "b", "c"])


class TestFakeBidfaxClient(unittest.TestCase):
    def test_known_lookup(self):
        c = FakeBidfaxClient(responses={"A": ("$1", "V", "u")})
        self.assertEqual(c.lookup("A"), ("$1", "V", "u"))

    def test_unknown_lookup(self):
        c = FakeBidfaxClient()
        self.assertEqual(c.lookup("Z"), (IN_PROGRESS, "", ""))

    def test_lookup_many(self):
        c = FakeBidfaxClient(responses={"A": ("$1", "V", "u")})
        result = c.lookup_many(["A", "B"])
        self.assertEqual(result["A"], ("$1", "V", "u"))
        self.assertEqual(result["B"], (IN_PROGRESS, "", ""))
        self.assertEqual(c.lookup_calls, ["A", "B"])

    def test_sale_ended_default_true(self):
        c = FakeBidfaxClient()
        self.assertTrue(c.sale_ended("some-url"))

    def test_sale_ended_per_url(self):
        c = FakeBidfaxClient(sale_ended={"u1": False, "u2": True})
        out = c.check_sale_ended_many(["u1", "u2", "u3"])
        self.assertEqual(out, {"u1": False, "u2": True, "u3": True})


class TestQueryWithRetriesMakeValidation(unittest.IsolatedAsyncioTestCase):
    """_query_with_retries must refuse to return data whose URL make doesn't
    match the expected make — regression guard for the Q5→Nissan Leaf bug."""

    async def _drive(self, page_search_results, expected_make):
        """Stub out the page + network helpers, then run one call."""
        import clients.bidfax as bf

        class FakePage:
            async def get(self, _url):
                await asyncio.sleep(0)
                return None

        # Replay canned (price, vin, url) tuples from `_search_once`
        results_iter = iter(page_search_results)

        async def fake_search_once(_page, _query):
            await asyncio.sleep(0)
            return next(results_iter)

        async def fake_wait_cf(_page):
            await asyncio.sleep(0)

        async def no_sleep(_s):
            # Use the pre-patch sleep so we don't recurse into ourselves.
            await _REAL_ASYNCIO_SLEEP(0)

        orig_search = bf._search_once
        orig_wait   = bf._wait_cf_clear
        orig_sleep  = bf.asyncio.sleep
        bf._search_once    = fake_search_once
        bf._wait_cf_clear  = fake_wait_cf
        bf.asyncio.sleep   = no_sleep
        try:
            return await bf._query_with_retries(FakePage(), "12345", expected_make)
        finally:
            bf._search_once   = orig_search
            bf._wait_cf_clear = orig_wait
            bf.asyncio.sleep  = orig_sleep

    async def test_matching_make_returns_result(self):
        out = await self._drive(
            [("$20,100", "VIN1", "https://bidfax.info/audi/q5/foo.html")],
            expected_make="AUDI",
        )
        self.assertEqual(out, ("$20,100", "VIN1", "https://bidfax.info/audi/q5/foo.html"))

    async def test_mismatched_make_all_three_retries_returns_not_found(self):
        """Q5→Nissan scenario: bidfax keeps returning a Nissan Leaf for a
        Q5 lot. After 3 mismatched retries we must return IN_PROGRESS,
        not hand back Nissan data."""
        nissan = ("$1,000", "NISSAN_VIN",
                  "https://bidfax.info/nissan/leaf/31169927-nissan-leaf-sv-vin-x.html")
        out = await self._drive([nissan, nissan, nissan], expected_make="AUDI")
        self.assertEqual(out, (IN_PROGRESS, "", ""))

    async def test_second_retry_matches_returned(self):
        mismatched = ("$1", "X", "https://bidfax.info/toyota/rav4/x.html")
        match      = ("$2", "Y", "https://bidfax.info/audi/q5/y.html")
        out = await self._drive([mismatched, match], expected_make="AUDI")
        self.assertEqual(out, match)

    async def test_no_expected_make_accepts_first_url(self):
        """VIN lookups don't know the make — don't reject based on URL."""
        nissan = ("$1", "V", "https://bidfax.info/nissan/leaf/x.html")
        out = await self._drive([nissan], expected_make="")
        self.assertEqual(out, nissan)

    async def test_empty_url_returns_not_found_immediately(self):
        out = await self._drive([(IN_PROGRESS, "", "")], expected_make="AUDI")
        self.assertEqual(out, (IN_PROGRESS, "", ""))


if __name__ == "__main__":
    unittest.main()
