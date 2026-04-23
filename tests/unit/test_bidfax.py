"""Unit tests for pure helpers in clients/bidfax.py."""

import json
import tempfile
import unittest
from pathlib import Path

from tests._helpers import ROOT  # noqa: F401  (forces sys.path setup)

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
        price, vin, url = extract_grid_result(html)
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


if __name__ == "__main__":
    unittest.main()
