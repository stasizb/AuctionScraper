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


class TestLogLookupResult(unittest.TestCase):
    """Per-lot progress lines so price_refresh / bidfax_info etc. can show
    'this lot got X' as it queries each one."""

    def _capture(self, *args, **kwargs):
        from clients.bidfax import _log_lookup_result
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _log_lookup_result(*args, **kwargs)
        return buf.getvalue()

    def test_found_line_includes_lot_price_vin_url(self):
        out = self._capture(
            3, 12, "50900496",
            ("$27,000", "JM3KKCHD2T1353518",
             "https://bidfax.info/mazda/cx-90/foo.html"),
        )
        self.assertIn("[bidfax 3/12]", out)
        self.assertIn("50900496",     out)
        self.assertIn("$27,000",      out)
        self.assertIn("JM3KKCHD2T1353518", out)
        self.assertIn("https://bidfax.info/mazda/cx-90/foo.html", out)

    def test_no_price_line_when_in_progress_and_no_url(self):
        out = self._capture(1, 1, "12345", (IN_PROGRESS, "", ""))
        self.assertIn("[bidfax 1/1]", out)
        self.assertIn("12345",        out)
        self.assertIn("No Price",     out)
        self.assertNotIn("https://",  out)

    def test_no_price_line_when_url_but_in_progress(self):
        # bidfax found a listing but the auction is still open
        out = self._capture(
            2, 5, "99999",
            (IN_PROGRESS, "", "https://bidfax.info/foo/bar/baz.html"),
        )
        self.assertIn("No Price",                              out)
        self.assertIn("https://bidfax.info/foo/bar/baz.html",  out)

    def test_em_dash_when_vin_missing(self):
        out = self._capture(
            1, 1, "X",
            ("$1", "", "https://bidfax.info/x/y/z.html"),
        )
        self.assertIn("VIN:—", out)


class TestSearchOncePollBudget(unittest.IsolatedAsyncioTestCase):
    """Regression: when Cloudflare's challenge stays up for several seconds,
    the grid-extraction polls must NOT be charged for those CF iterations.
    The old code (`if i >= 5: return IN_PROGRESS`) bailed after 6 raw
    iterations regardless of CF state — so a slow CF clear left zero polls
    to find the grid, and bidfax results were silently dropped."""

    async def _drive(self, page_html_sequence, recaptcha_token: str = "valid-token"):
        """Simulate `page.get_content()` returning each item in sequence.

        `recaptcha_token` controls what `_wait_for_recaptcha_token`'s
        page.evaluate('…token2…') returns. Default is non-empty so the
        existing tests don't have to care about the reCAPTCHA wait.
        """
        import clients.bidfax as bf

        seq = iter(page_html_sequence)

        class FakePage:
            url = "https://bidfax.info/results/foo"
            async def get(self, _url):
                await _REAL_ASYNCIO_SLEEP(0)
            async def find(self, _sel):
                # _fill_and_submit needs both #search and #submit to exist
                await _REAL_ASYNCIO_SLEEP(0)
                class _El:
                    async def click(self): await _REAL_ASYNCIO_SLEEP(0)
                    async def send_keys(self, _v): await _REAL_ASYNCIO_SLEEP(0)
                return _El()
            async def evaluate(self, _js):
                # _wait_for_recaptcha_token reads the page-side token2 value
                await _REAL_ASYNCIO_SLEEP(0)
                return recaptcha_token
            async def get_content(self):
                await _REAL_ASYNCIO_SLEEP(0)
                try:    return next(seq)
                except StopIteration: return ""

        async def fast_sleep(_s): await _REAL_ASYNCIO_SLEEP(0)
        # Suppress the on-disk diagnostic dump so tests don't leave artefacts.
        def quiet_dump(*_args, **_kwargs): return None
        orig_sleep = bf.asyncio.sleep
        orig_dump  = bf._dump_empty_search
        bf.asyncio.sleep      = fast_sleep
        bf._dump_empty_search = quiet_dump
        try:
            return await bf._search_once(FakePage(), "44602912")
        finally:
            bf.asyncio.sleep      = orig_sleep
            bf._dump_empty_search = orig_dump

    async def test_grid_after_long_cloudflare_still_found(self):
        """6 CF-loading polls then the grid arrives — must NOT bail early."""
        cf      = "<html>cf_chl loading…</html>"
        success = """
        <div id='grid'>
          <span class='prices'>27000</span>
          <a href='https://bidfax.info/honda/cr-v/foo-vin-jm3kkchd2t1353518.html'>x</a>
        </div>
        """
        # 6 CF polls then the grid — old code bailed at i >= 5 regardless
        out = await self._drive([cf]*6 + [success])
        self.assertEqual(out[0], "$27,000")
        self.assertIn("bidfax.info/honda/cr-v", out[2])

    async def test_no_grid_after_full_budget_returns_in_progress(self):
        """If the grid never appears within the budget, bail with IN_PROGRESS."""
        empty_post_cf = "<html><body>no grid</body></html>"
        out = await self._drive([empty_post_cf] * 30)
        self.assertEqual(out, (IN_PROGRESS, "", ""))

    async def test_homepage_bounce_after_submit_bails_immediately(self):
        """Regression for the May 2026 reCAPTCHA-bounce issue: bidfax sometimes
        accepts the URL transition (querystring) but re-renders the homepage
        because the reCAPTCHA token wasn't validated. The homepage HTML
        contains `id="search"` (the search input). Detect it and bail
        immediately — don't waste the full grid-poll budget."""
        homepage = '<html><body><input type="text" id="search"></body></html>'
        # Even a single homepage-content poll should cause an immediate bail.
        out = await self._drive([homepage] + ["<html>x</html>"] * 30)
        self.assertEqual(out, (IN_PROGRESS, "", ""))

    async def test_empty_recaptcha_token_aborts_submission(self):
        """When the reCAPTCHA token never populates, _fill_and_submit must
        return False (search aborted) instead of submitting an empty form
        which would silently bounce back to the homepage."""
        # _fill_and_submit aborts before any get_content polling, so the
        # html sequence is irrelevant. Token is empty → wait times out.
        out = await self._drive(["<html>shouldn't reach here</html>"],
                                recaptcha_token="")
        self.assertEqual(out, (IN_PROGRESS, "", ""))


class TestWaitForRecaptchaToken(unittest.IsolatedAsyncioTestCase):
    """Unit tests for the reCAPTCHA-token wait helper itself."""

    async def _run(self, evaluate_returns, cap_seconds: float = 0.05):
        import clients.bidfax as bf

        results = iter(evaluate_returns)

        class FakePage:
            async def evaluate(self, _js):
                await _REAL_ASYNCIO_SLEEP(0)
                try:    return next(results)
                except StopIteration: return ""

        async def fast_sleep(_s): await _REAL_ASYNCIO_SLEEP(0)
        orig = bf.asyncio.sleep
        bf.asyncio.sleep = fast_sleep
        try:
            return await bf._wait_for_recaptcha_token(FakePage(), timeout=cap_seconds)
        finally:
            bf.asyncio.sleep = orig

    async def test_token_present_immediately(self):
        self.assertTrue(await self._run(["valid-token"]))

    async def test_token_appears_after_a_few_polls(self):
        # First few polls return empty, then a token shows up
        self.assertTrue(await self._run(["", "", "", "valid-token"], cap_seconds=10.0))

    async def test_token_never_appears_returns_false(self):
        self.assertFalse(await self._run([""] * 100, cap_seconds=0.5))

    async def test_evaluate_raising_is_treated_as_empty(self):
        """If page.evaluate throws (page not ready), keep polling."""
        import clients.bidfax as bf
        attempts = {"n": 0}

        class FakePage:
            async def evaluate(self, _js):
                await _REAL_ASYNCIO_SLEEP(0)
                attempts["n"] += 1
                if attempts["n"] < 3:
                    raise RuntimeError("page not ready")
                return "valid-token"

        async def fast_sleep(_s): await _REAL_ASYNCIO_SLEEP(0)
        orig = bf.asyncio.sleep
        bf.asyncio.sleep = fast_sleep
        try:
            self.assertTrue(await bf._wait_for_recaptcha_token(FakePage(), timeout=10.0))
        finally:
            bf.asyncio.sleep = orig
        self.assertGreaterEqual(attempts["n"], 3)


if __name__ == "__main__":
    unittest.main()
