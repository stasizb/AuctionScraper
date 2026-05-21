#!/usr/bin/env python3
"""
BidfaxClient abstraction — wraps every interaction with bidfax.info.

  - BidfaxClient         — the interface scripts depend on
  - BrowserBidfaxClient  — real implementation using nodriver
  - FakeBidfaxClient     — in-memory test double

The interface is batch-oriented. Each method runs its own async session
internally (one asyncio.run per batch), matching nodriver's loop-scoped
object model.  Single-shot `lookup()` / `sale_ended()` helpers are thin
wrappers over the batch methods.

Also re-exports the shared cache helpers (load_cache / save_cache), the
pure HTML-parsing function (extract_grid_result), and the high-level
`run_batch` / `run_batch_vins` convenience wrappers used by the scripts.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.concurrency import (  # DEFAULT_* kept for back-compat with importers
    BIDFAX_TAB_CONCURRENCY,
    DEFAULT_TAB_CONCURRENCY,  # noqa: F401  (re-exported)
)
from core.debug       import DEBUG_SCREENSHOTS
from core.job_log     import job_log

try:
    import nodriver as uc
    from bs4 import BeautifulSoup
    _DEPS_OK = True
except ImportError:
    _DEPS_OK = False


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

BIDFAX_HOME     = "https://bidfax.info"
IN_PROGRESS     = "In Progress"

_CF_WAIT_TIMEOUT    = 30.0

_BIDFAX_HOME_PATH = re.compile(r'^https?://bidfax\.info/?$')
# RESULT_URL_RE / VIN_FROM_URL_RE live in bidfax_parsing — re-exported below
# under their old leading-underscore names for any callers / tests that
# reach in for them.


# ---------------------------------------------------------------------------
# Cache helpers — re-exported from clients.bidfax_cache so the cache module
# stays browser-free (no nodriver dep) and can be unit-tested in isolation.
# ---------------------------------------------------------------------------

from clients.bidfax_cache   import (   # noqa: E402
    DEFAULT_CACHE_TTL_DAYS,
    _TIMESTAMPS_KEY,
    cache_results,
    load_cache,
    save_cache,
)
from clients.bidfax_parsing import (   # noqa: E402
    extract_grid_result,
    url_make_matches,
    RESULT_URL_RE   as _RESULT_URL_RE,
    VIN_FROM_URL_RE as _VIN_FROM_URL_RE,
)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class BidfaxClient(Protocol):
    """Batch-oriented interface over bidfax.info."""

    def lookup_many(
        self,
        queries: list[str],
        makes: dict[str, str] | None = None,
        delay: float = 2.0,
        max_concurrent: int = BIDFAX_TAB_CONCURRENCY,
    ) -> dict[str, tuple[str, str, str]]:
        """Search each query. Returns {query: (price, vin, url)}.

        `makes` enables per-query URL-make retry (up to 3x).
        `max_concurrent` > 1 runs lookups in parallel across that many tabs
        (experimental — Cloudflare may react to burst traffic).
        """

    def check_sale_ended_many(
        self,
        lot_urls: list[str],
    ) -> dict[str, bool]:
        """Open each Copart lot page; return {url: sale_ended_bool}."""

    # Convenience single-shot wrappers (default impls — subclasses can override)
    def lookup(self, query: str, expected_make: str = "") -> tuple[str, str, str]:
        makes = {query: expected_make} if expected_make else None
        return self.lookup_many([query], makes=makes).get(query, (IN_PROGRESS, "", ""))

    def sale_ended(self, lot_url: str) -> bool:
        return self.check_sale_ended_many([lot_url]).get(lot_url, False)


# ---------------------------------------------------------------------------
# Real (browser-backed) implementation
# ---------------------------------------------------------------------------

class BrowserBidfaxClient:
    """Live bidfax.info client backed by nodriver.

    Every public method manages its own browser lifecycle inside a single
    asyncio.run(). Set `browser_port` to attach to an already-running Chrome
    (shared session across pipeline steps)."""

    def __init__(self, browser_port: int | None = None) -> None:
        if not _DEPS_OK:
            raise RuntimeError("nodriver + beautifulsoup4 required. "
                               "Install with:  pip install nodriver beautifulsoup4 lxml")
        self.browser_port = browser_port

    # ---- Public interface --------------------------------------------------

    def lookup_many(
        self,
        queries: list[str],
        makes: dict[str, str] | None = None,
        delay: float = 2.0,
        max_concurrent: int = BIDFAX_TAB_CONCURRENCY,
    ) -> dict[str, tuple[str, str, str]]:
        if not queries:
            return {}
        return asyncio.run(
            self._lookup_many_async(queries, makes or {}, delay, max_concurrent)
        )

    def check_sale_ended_many(self, lot_urls: list[str]) -> dict[str, bool]:
        """Determine sale-ended state for each Copart lot URL.

        Now uses Copart's public search API rather than opening a browser
        per lot: a lot whose number is absent from the search index has
        had its auction concluded. The Playwright cookie warmup is shared
        with copart_search.py via clients.copart_session, so the daily
        run typically pays the browser cost exactly once. Legacy
        nodriver-based implementation is in clients/bidfax.py.bak.
        """
        if not lot_urls:
            return {}
        # Local import: keeps this module importable without playwright
        # installed (the actual Playwright call is inside copart_session).
        from clients.copart_session import get_or_warmup_session
        from clients.copart         import check_sale_ended_via_search
        session = get_or_warmup_session()
        return check_sale_ended_via_search(session, lot_urls)

    def lookup(self, query: str, expected_make: str = "") -> tuple[str, str, str]:
        makes = {query: expected_make} if expected_make else None
        return self.lookup_many([query], makes=makes).get(query, (IN_PROGRESS, "", ""))

    def sale_ended(self, lot_url: str) -> bool:
        return self.check_sale_ended_many([lot_url]).get(lot_url, False)

    # ---- Async internals ---------------------------------------------------

    # Number of attempts when launching a fresh Chrome via nodriver.
    # `uc.start()` occasionally fails with "Failed to connect to browser"
    # — Chrome spawns OK but the CDP WebSocket handshake misses its
    # internal timeout window. We've seen this 1-in-N times on macOS
    # under both Copart-API recon and the daily bidfax phase. A short
    # retry consistently clears it because the next attempt spawns a
    # fresh Chrome process that doesn't race the handshake.
    _BROWSER_START_ATTEMPTS = 3
    _BROWSER_START_BACKOFF_S = 2.0

    async def _start_browser(self):
        if self.browser_port:
            return await uc.start(host="127.0.0.1", port=self.browser_port)
        last_exc: Exception | None = None
        for attempt in range(1, self._BROWSER_START_ATTEMPTS + 1):
            try:
                return await uc.start(
                    headless=False, sandbox=False,
                    browser_args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
            except Exception as exc:
                last_exc = exc
                print(f"    [bidfax] nodriver Chrome start failed "
                      f"(attempt {attempt}/{self._BROWSER_START_ATTEMPTS}): "
                      f"{exc.__class__.__name__}: {str(exc).strip().splitlines()[0] if str(exc).strip() else exc!r}",
                      flush=True)
                if attempt < self._BROWSER_START_ATTEMPTS:
                    await asyncio.sleep(self._BROWSER_START_BACKOFF_S)
        # Out of retries — re-raise the last exception so the caller
        # gets the original "Failed to connect to browser" detail.
        assert last_exc is not None
        raise last_exc

    async def _stop_browser(self, browser) -> None:
        try:
            await asyncio.wait_for(browser.stop(), timeout=5.0)
        except Exception:
            pass

    async def _lookup_many_async(
        self,
        queries: list[str],
        makes: dict[str, str],
        delay: float,
        max_concurrent: int = BIDFAX_TAB_CONCURRENCY,
    ) -> dict[str, tuple[str, str, str]]:
        browser = await self._start_browser()
        try:
            if max_concurrent <= 1:
                return await self._lookup_sequential(browser, queries, makes, delay)
            return await self._lookup_parallel(browser, queries, makes, max_concurrent)
        finally:
            await self._stop_browser(browser)

    async def _lookup_sequential(
        self, browser, queries: list[str], makes: dict[str, str], delay: float,
    ) -> dict[str, tuple[str, str, str]]:
        results: dict[str, tuple] = {}
        page  = await browser.get(BIDFAX_HOME)
        total = len(queries)
        await _wait_cf_clear(page)
        for i, q in enumerate(queries, 1):
            result = await _query_with_retries(page, q, makes.get(q, ""))
            results[q] = result
            _log_lookup_result(i, total, q, result)
            if i < total:
                await asyncio.sleep(delay)
        return results

    async def _lookup_parallel(
        self, browser, queries: list[str], makes: dict[str, str], max_concurrent: int,
    ) -> dict[str, tuple[str, str, str]]:
        """Run lookups across up to `max_concurrent` tabs concurrently.

        Experimental: Cloudflare may challenge burst traffic, so each worker
        holds a permit from the semaphore for its full retry cycle.
        """
        sem      = asyncio.Semaphore(max_concurrent)
        total    = len(queries)
        progress = {"done": 0}

        async def _worker(q: str) -> tuple[str, tuple[str, str, str]]:
            async with sem, job_log():
                tab = await browser.get(BIDFAX_HOME, new_tab=True)
                try:
                    await _wait_cf_clear(tab)
                    result = await _query_with_retries(tab, q, makes.get(q, ""))
                finally:
                    try:
                        await tab.close()
                    except Exception:
                        pass
                progress["done"] += 1
                _log_lookup_result(progress["done"], total, q, result)
                return q, result

        pairs = await asyncio.gather(*(_worker(q) for q in queries))
        return dict(pairs)

# ---------------------------------------------------------------------------
# Fake (in-memory) implementation for tests
# ---------------------------------------------------------------------------

class FakeBidfaxClient:
    """In-memory BidfaxClient. Returns canned lookup/sale-ended responses."""

    def __init__(
        self,
        responses: dict[str, tuple[str, str, str]] | None = None,
        sale_ended: dict[str, bool] | None = None,
        default_sale_ended: bool = True,
    ) -> None:
        self.responses           = dict(responses or {})
        self._sale_ended         = dict(sale_ended or {})
        self._default_sale_ended = default_sale_ended
        self.lookup_calls:     list[str] = []
        self.sale_ended_calls: list[str] = []

    def lookup_many(
        self,
        queries: list[str],
        makes: dict[str, str] | None = None,
        delay: float = 2.0,
        max_concurrent: int = BIDFAX_TAB_CONCURRENCY,
    ) -> dict[str, tuple[str, str, str]]:
        del makes, delay, max_concurrent  # accepted for protocol parity; fake ignores
        self.lookup_calls.extend(queries)
        return {q: self.responses.get(q, (IN_PROGRESS, "", "")) for q in queries}

    def check_sale_ended_many(self, lot_urls: list[str]) -> dict[str, bool]:
        self.sale_ended_calls.extend(lot_urls)
        return {u: self._sale_ended.get(u, self._default_sale_ended) for u in lot_urls}

    def lookup(self, query: str, expected_make: str = "") -> tuple[str, str, str]:
        makes = {query: expected_make} if expected_make else None
        return self.lookup_many([query], makes=makes).get(query, (IN_PROGRESS, "", ""))

    def sale_ended(self, lot_url: str) -> bool:
        return self.check_sale_ended_many([lot_url]).get(lot_url, False)


# ---------------------------------------------------------------------------
# High-level cache-aware wrappers
# ---------------------------------------------------------------------------

def run_batch(
    queries: list[str],
    delay: float,
    cache_path: Path,
    makes: dict[str, str] | None = None,
    browser_port: int | None = None,
    client: BidfaxClient | None = None,
    max_concurrent: int = BIDFAX_TAB_CONCURRENCY,
) -> dict[str, tuple]:
    """Search bidfax for each query, using disk cache to skip known results.

    Only final (non-"In Progress") prices are cached.  `max_concurrent` > 1
    fans out across that many tabs (experimental, see BidfaxClient.lookup_many).
    """
    if client is None and not _DEPS_OK:
        print("[warn] nodriver/bs4 not installed — skipping bidfax lookups.")
        return {q: (IN_PROGRESS, "", "") for q in queries}

    cache    = load_cache(cache_path, ttl_days=DEFAULT_CACHE_TTL_DAYS)
    to_fetch = [q for q in queries if q not in cache or q == _TIMESTAMPS_KEY]

    if to_fetch:
        print(f"[*] bidfax lookup: {len(to_fetch)} new  (cached: {len(cache)})")
        real_client = client or BrowserBidfaxClient(browser_port=browser_port)
        fetched     = real_client.lookup_many(
            to_fetch, makes=makes, delay=delay, max_concurrent=max_concurrent,
        )
        cache = cache_results(cache_path, fetched)

    return {q: cache.get(q, (IN_PROGRESS, "", "")) for q in queries
            if q != _TIMESTAMPS_KEY}


def run_batch_vins(
    vins: list[str],
    delay: float,
    cache_path: Path,
    browser_port: int | None = None,
    client: BidfaxClient | None = None,
    max_concurrent: int = BIDFAX_TAB_CONCURRENCY,
) -> dict[str, str]:
    """Search bidfax.info for each VIN, returning {vin: url}. Disk-cached."""
    if client is None and not _DEPS_OK:
        return dict.fromkeys(vins, "")

    cache = load_cache(cache_path)

    def _cached_url(vin: str) -> str:
        entry = cache.get(vin)
        if isinstance(entry, (list, tuple)) and len(entry) >= 3:
            return entry[2]
        if isinstance(entry, str):
            return entry
        return ""

    to_fetch = [v for v in vins if not _cached_url(v)]

    if to_fetch:
        print(f"[*] bidfax VIN lookup: {len(to_fetch)} new  (cached: {len(vins) - len(to_fetch)})")
        real_client = client or BrowserBidfaxClient(browser_port=browser_port)
        fetched     = real_client.lookup_many(
            to_fetch, delay=delay, max_concurrent=max_concurrent,
        )
        cache.update({v: ("", v, url) for v, (_p, _vin, url) in fetched.items() if url})
        save_cache(cache_path, cache)

    return {v: _cached_url(v) for v in vins}


# ---------------------------------------------------------------------------
# Async browser helpers (private — only used by BrowserBidfaxClient)
# ---------------------------------------------------------------------------

def _dump_token_missing(query: str, html: str) -> None:
    """Save a page snapshot when CSRF-token harvest gives up.

    Tells us at a glance whether (a) Cloudflare is still showing its
    challenge, (b) the page rendered but the rel='alternate' link is gone,
    or (c) the link is there but its href no longer carries token2.
    """
    if not html:
        return
    try:
        from datetime import datetime
        log_dir = Path(__file__).resolve().parent.parent / "logs"
        log_dir.mkdir(exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = log_dir / f"bidfax_token_missing_{query}_{ts}.html"
        path.write_text(html[:30_000], encoding="utf-8")
        print(f"    [bidfax] token-missing snapshot for {query} → {path}",
              flush=True)
    except Exception as e:
        print(f"    [bidfax] could not save token-missing snippet: {e}",
              flush=True)


async def _save_query_screenshot(page, query: str) -> None:
    """Snap the bidfax page after each query attempt — for debugging cases
    like 'bidfax returns empty for a lot we know it has'.

    One file per try (timestamped), so a lot that retried 3 times produces
    3 distinct screenshots. Errors are swallowed — must never break lookup.
    Gated on `DEBUG_SCREENSHOTS` (env / .env): off by default.
    """
    if not DEBUG_SCREENSHOTS:
        return
    from datetime import datetime
    log_dir = Path(__file__).resolve().parent.parent / "logs"
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]   # ms precision
    path = log_dir / f"bidfax_screenshot_{query}_{ts}.png"
    try:
        log_dir.mkdir(exist_ok=True)
        await page.save_screenshot(filename=str(path), format="png", full_page=True)
        print(f"    [bidfax] screenshot for {query} → {path.name}", flush=True)
    except Exception as exc:
        print(f"    [bidfax] [warn] screenshot failed for {query}: {exc}",
              flush=True)


def _dump_empty_search(query: str, html: str) -> None:
    """Save a snippet of the page when bidfax search came back empty.

    The grid-extraction logic returning None after Cloudflare cleared usually
    means 'no result on bidfax' — but it can also mean the page shape changed
    (selector drift) or Cloudflare blocked us in a soft way. Either case is
    much easier to investigate when we have the actual HTML on disk.
    """
    if not html:
        return
    try:
        from datetime import datetime
        log_dir = Path(__file__).resolve().parent.parent / "logs"
        log_dir.mkdir(exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = log_dir / f"bidfax_empty_{query}_{ts}.html"
        # Cap at ~30KB — enough to inspect the page structure without
        # filling the disk on a long batch where many lots return empty.
        path.write_text(html[:30_000], encoding="utf-8")
        print(f"    [bidfax] empty result for {query} — page snippet → {path}",
              flush=True)
    except Exception as e:
        print(f"    [bidfax] could not save empty-result snippet: {e}", flush=True)


def _log_lookup_result(idx: int, total: int, query: str, result: tuple[str, str, str]) -> None:
    """Print one progress line per bidfax lookup.

    Format:
        [bidfax 3/12] 50900496 → $27,000  VIN:JM3K…  https://bidfax.info/...
        [bidfax 3/12] 50900496 → No Price
    The 'No Price' branch covers both 'bidfax has no result' and 'every retry
    came back with the wrong make' (both surface as IN_PROGRESS, "", "").
    """
    price, vin, url = result
    if url and price != IN_PROGRESS:
        print(f"  [bidfax {idx}/{total}] {query} → {price}  "
              f"VIN:{vin or '—'}  {url}", flush=True)
    elif url:
        # URL but no final price (sale still open on bidfax)
        print(f"  [bidfax {idx}/{total}] {query} → No Price  ({url})", flush=True)
    else:
        print(f"  [bidfax {idx}/{total}] {query} → No Price", flush=True)


async def _wait_cf_clear(page) -> None:
    async def _poll() -> None:
        while True:
            await asyncio.sleep(1)
            if "cf_chl" not in await page.get_content():
                return
    try:
        await asyncio.wait_for(_poll(), timeout=_CF_WAIT_TIMEOUT)
    except asyncio.TimeoutError:
        pass


_FORM_TOKEN_TIMEOUT = 15.0   # seconds — reCAPTCHA v3 can take a few seconds


# Bidfax's search form has two hidden fields, `#token2` (a reCAPTCHA v3
# token bound to the CURRENT browser session) and `#action2` (the static
# string "search_action"). Both must be populated before submit, otherwise
# the server silently bounces back to the homepage.
#
# Token sources, in order:
#   1. The field already has a value because bidfax's own dle_js.js fired
#      grecaptcha.execute() on page-load and stashed the token. Use it.
#   2. Otherwise we fire grecaptcha.execute() ourselves with the site key
#      lifted from the page's recaptcha/api.js?render=… URL and action
#      "search_action". The promise resolves into #token2 asynchronously;
#      the outer Python loop polls for the value to appear.
#
# Notes / regression history:
#   * The token embedded in `<link rel="alternate" href="...token2=...">` is
#     for SEO/hreflang URLs and is bound to a different session — the
#     server REJECTS submissions that use it. An earlier version of this
#     harvest preferred that path; on 2026-05-04 bidfax restored those
#     embedded tokens to the page and our preferred-path-2 logic started
#     poisoning every submission. Keep that path OUT.
_FORM_TOKEN_HARVEST_JS = """
(function() {
    var t = document.getElementById('token2');
    var a = document.getElementById('action2');
    if (t && t.value) {
        // bidfax JS already filled the token; make sure action2 is set too.
        if (a && !a.value) a.value = 'search_action';
        return t.value;
    }
    // Fire reCAPTCHA v3 ourselves; .then() fills #token2 + #action2 async.
    if (!window._auctionsRecaptchaTriggered && typeof grecaptcha !== 'undefined') {
        var siteKey = null;
        var scripts = document.querySelectorAll('script[src*="recaptcha/api.js"]');
        for (var i = 0; i < scripts.length; i++) {
            var m = scripts[i].src.match(/[?&]render=([^&]+)/);
            if (m) { siteKey = m[1]; break; }
        }
        if (siteKey) {
            try {
                window._auctionsRecaptchaTriggered = true;
                grecaptcha.ready(function() {
                    grecaptcha
                        .execute(siteKey, {action: 'search_action'})
                        .then(function(token) {
                            if (t) t.value = token;
                            if (a) a.value = 'search_action';
                        });
                });
            } catch (e) { /* nothing to do */ }
        }
    }
    return t && t.value ? t.value : '';
})();
"""


async def _ensure_search_tokens(page, timeout: float = _FORM_TOKEN_TIMEOUT) -> bool:
    """Make sure bidfax's hidden #token2 / #action2 fields are populated.

    Strategy:
      1. Poll for natural population (gives bidfax's own JS first crack).
      2. After every poll, copy the CSRF tokens from the rel="alternate"
         link into the form fields directly. The same JS runs whether the
         input was empty or already filled — it's idempotent and a no-op
         once #token2 has a value.

    Returns True iff #token2 ends up non-empty.
    """
    elapsed = 0.0
    while elapsed < timeout:
        try:
            val = await page.evaluate(_FORM_TOKEN_HARVEST_JS)
        except Exception:
            val = None
        if val:
            return True
        await asyncio.sleep(0.5)
        elapsed += 0.5
    return False


async def _fill_and_submit(page, query: str) -> bool:
    search_input = await page.find("#search")
    if not search_input:
        return False
    # Reset any state left over from a previous search on this page —
    # bidfax's #search form lives on result pages too, so we reuse the
    # page across lots instead of reloading. Specifically:
    #   - #search may still hold the previous query text → send_keys
    #     would append to it.
    #   - #token2 still holds the previous reCAPTCHA token (single-use,
    #     server rejects reuse), so the harvest JS would short-circuit
    #     and return the stale value. Clearing + resetting the trigger
    #     gate forces a fresh grecaptcha.execute() this round.
    try:
        await page.evaluate(
            "(function(){"
            "var i=document.getElementById('search');"
            "if(i){i.value='';i.dispatchEvent(new Event('input',{bubbles:true}));}"
            "var t=document.getElementById('token2');  if(t) t.value='';"
            "var a=document.getElementById('action2'); if(a) a.value='';"
            "window._auctionsRecaptchaTriggered=false;"
            "})()"
        )
    except Exception:
        # First search on a brand-new tab has no stale state to clear;
        # the script can silently no-op there.
        pass
    await asyncio.sleep(0.3)
    await search_input.click()
    await asyncio.sleep(0.5)
    await search_input.send_keys(query)
    await asyncio.sleep(0.5)

    if not await _ensure_search_tokens(page):
        # Capture the page so we can see what the harvest JS was looking at
        # when it gave up — could be Cloudflare not really cleared, or a DOM
        # change that broke the alternate-link selector.
        try:
            html = await page.get_content()
        except Exception:
            html = ""
        _dump_token_missing(query, html)
        print(f"    [bidfax] CSRF tokens missing for {query!r} "
              f"— aborting submit (server would silently bounce back to home)",
              flush=True)
        return False

    submit_btn = await page.find("#submit")
    if not submit_btn:
        return False
    await submit_btn.click()
    return True


async def _wait_for_navigation(page, start_url: str = "") -> bool:
    """Wait up to ~10s for `page.url` to differ from `start_url`.

    `start_url` lets us detect navigation when reusing the same page
    across multiple lots (we may start on a previous result URL, not on
    the homepage). When `start_url` is empty, falls back to the legacy
    'wait until not on homepage' check so we stay correct for any old
    callers."""
    for _ in range(10):
        await asyncio.sleep(1)
        try:
            current_url = page.url or ""
        except Exception:
            current_url = ""
        if not current_url:
            continue
        if start_url:
            if current_url != start_url:
                return True
        else:
            if not _BIDFAX_HOME_PATH.match(current_url):
                return True
    return False


_GRID_POLL_BUDGET     = 10   # polls (1s each) AFTER Cloudflare clears
_TOTAL_POLL_HARD_CAP  = 30   # safety net so we never spin forever

class _BidfaxBounce(Exception):
    """Server accepted the URL transition but rendered the homepage instead
    of search results — usually a stale or low-scored reCAPTCHA v3 token.

    Raised by `_search_once` so `_query_with_retries` can distinguish a
    transient bounce (worth retrying with a fresh page + fresh token) from
    a true 'no result on bidfax' (don't retry — bidfax just doesn't have
    this lot indexed yet)."""


async def _search_once(page, query: str) -> tuple[str, str, str]:
    """Perform one bidfax search on an existing page. Returns (price, vin, url).

    Polls the page in 1-second steps. Iterations where Cloudflare is still
    showing its challenge ("cf_chl" present) don't count against the
    grid-extraction budget — otherwise a slow CF challenge consumes all the
    polls and we bail before the result grid has a chance to render.

    Raises `_BidfaxBounce` when the server redirects us back to the
    homepage after submit (reCAPTCHA score too low / rate-limit). The
    caller can decide whether to retry.

    The page is reused across lots: we don't reload `BIDFAX_HOME` between
    queries, we just clear the previous query state in `_fill_and_submit`
    and submit a fresh search. So bounce detection looks at the final URL
    (= homepage) rather than at page HTML.
    """
    try:
        try:
            start_url = page.url or ""
        except Exception:
            start_url = ""

        if not await _fill_and_submit(page, query):
            return IN_PROGRESS, "", ""
        navigated = await _wait_for_navigation(page, start_url)

        try:
            cur_url = page.url or ""
        except Exception:
            cur_url = ""

        # Bounce: post-submit URL is back at the homepage. This catches
        # both forms — full redirect from a previous result back to '/',
        # and same-page re-render on the homepage (which leaves URL='/').
        if cur_url and _BIDFAX_HOME_PATH.match(cur_url):
            try:
                bounce_html = await page.get_content()
            except Exception:
                bounce_html = ""
            _dump_empty_search(query, bounce_html)
            raise _BidfaxBounce(query)

        if not navigated:
            # Didn't navigate AND not at home — silent rejection, can't
            # make progress. Bail without a screenshot dump (rare).
            return IN_PROGRESS, "", ""

        polls_after_cf = 0
        last_html      = ""
        for _ in range(_TOTAL_POLL_HARD_CAP):
            await asyncio.sleep(1)
            last_html = await page.get_content()
            if "cf_chl" in last_html:
                continue
            result = extract_grid_result(last_html)
            if result is not None:
                return result
            polls_after_cf += 1
            if polls_after_cf >= _GRID_POLL_BUDGET:
                break

        # Search reached this point with CF cleared but no grid result —
        # most likely bidfax has nothing for `query`, but it could also be a
        # page-shape change (different result-URL pattern, missing #grid).
        # Dump a snippet so the next failure is debuggable instead of silent.
        _dump_empty_search(query, last_html)
        return IN_PROGRESS, "", ""
    finally:
        # Snap the page on every exit (success, empty, bounce, raised exc)
        # so we have a visual record of what bidfax actually rendered.
        await _save_query_screenshot(page, query)


# Cap on attempts per failure mode. Bounces are NOT retried — when bidfax
# returns the homepage with the generic "empty search" alert, it's almost
# always a soft-block (rate limit / reCAPTCHA score depletion) and a second
# query from the same session goes deeper into the soft-block, not out of
# it. Better to surface IN_PROGRESS once and let a future, cleaner-session
# run pick it up than to waste a retry that risks blocking the whole batch.
_BOUNCE_MAX_ATTEMPTS    = 1     # initial only — no retry
_BOUNCE_RETRY_WAIT      = 5.0   # unused while MAX_ATTEMPTS=1, kept for tests

# Make-mismatch retries are different — bidfax DID accept the search and
# returned a result, just for the wrong vehicle. Retrying often surfaces
# the correct lot on the next try, so keep the budget generous.
_MISMATCH_MAX_ATTEMPTS  = 3


async def _query_with_retries(page, query: str, expected_make: str) -> tuple[str, str, str]:
    """Run one bidfax search on the supplied page; retry on two specific
    failure modes.

      * Homepage bounce — the server rejected our submission (low reCAPTCHA
        score, rate-limit, or bidfax simply doesn't have this lot). One
        retry after a 5s pause handles the transient rate-limit case;
        more retries usually just confirm "not indexed".
      * Wrong-make URL — bidfax returned a result for a different vehicle.
        Up to 2 retries (3 attempts) — these are cheap (no backoff) and
        the next bidfax hit is often correct.

    A genuine empty-grid "no result" (URL absent but not a bounce) returns
    immediately — bidfax simply doesn't index that lot yet.

    The page is NOT reloaded here. Caller is expected to have already
    landed it on bidfax (homepage on first call; previous result page on
    subsequent calls — bidfax's search form lives on result pages too, so
    we reuse it). `_fill_and_submit` clears stale form state before each
    submission; the per-iteration `await page.get(BIDFAX_HOME)` of older
    versions is gone (saves a full reload + Cloudflare wait per lot).
    """
    bounce_attempts   = 0
    mismatch_attempts = 0
    while True:
        try:
            price, vin, url = await _search_once(page, query)
        except _BidfaxBounce:
            bounce_attempts += 1
            if bounce_attempts >= _BOUNCE_MAX_ATTEMPTS:
                print(f"    [bidfax] {query!r}: server bounced back to "
                      f"homepage (soft-block / not indexed) — "
                      f"surfacing IN_PROGRESS without retry "
                      f"(see _BOUNCE_MAX_ATTEMPTS comment)", flush=True)
                return IN_PROGRESS, "", ""
            await asyncio.sleep(_BOUNCE_RETRY_WAIT)
            continue

        if not url:
            # Genuine "no result" — surface as not-found, don't waste retries.
            return IN_PROGRESS, "", ""

        if not expected_make or url_make_matches(expected_make, url):
            return price, vin, url

        mismatch_attempts += 1
        if mismatch_attempts >= _MISMATCH_MAX_ATTEMPTS:
            # All retries surfaced the wrong vehicle — refuse to fabricate a price.
            return IN_PROGRESS, "", ""
        print(f"    [bidfax] make mismatch for {query!r}: "
              f"expected {expected_make!r}, got URL {url} "
              f"— retrying ({mismatch_attempts}/{_MISMATCH_MAX_ATTEMPTS - 1})",
              flush=True)
