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
import json
import re
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

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
SALE_ENDED_TEXT = "Sale ended"

_CF_WAIT_TIMEOUT    = 30.0
_COPART_RENDER_WAIT = 4.0
_MAX_CONCURRENT     = 5

_BIDFAX_HOME_PATH = re.compile(r'^https?://bidfax\.info/?$')
_RESULT_URL_RE    = re.compile(r'^https://bidfax\.info/[^/]+/[^/]+/.+\.html$')
_VIN_FROM_URL_RE  = re.compile(r'-vin-([a-z0-9]+)\.html$', re.IGNORECASE)


# ---------------------------------------------------------------------------
# Cache helpers (pure)
# ---------------------------------------------------------------------------

def load_cache(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {k: tuple(v) if isinstance(v, list) else v for k, v in data.items()}
        except ValueError:
            return {}
    return {}


def save_cache(path: Path, cache: dict) -> None:
    serialisable = {k: list(v) if isinstance(v, tuple) else v for k, v in cache.items()}
    path.write_text(json.dumps(serialisable, indent=2, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# URL / HTML parsing (pure)
# ---------------------------------------------------------------------------

def url_make_matches(csv_make: str, bidfax_url: str) -> bool:
    parts    = bidfax_url.replace("https://bidfax.info/", "").split("/")
    url_make = parts[0].lower() if parts else ""
    norm     = re.sub(r"[\s_]+", "-", csv_make.strip().lower())
    return bool(url_make) and (url_make == norm
                                or norm.startswith(url_make)
                                or url_make.startswith(norm))


def extract_grid_result(html: str) -> tuple[str, str, str] | None:
    """Parse bidfax results-page HTML. Returns (price, vin, url) or None."""
    if not _DEPS_OK:
        return None
    soup = BeautifulSoup(html, "lxml")
    grid = soup.find(id="grid")
    if not grid:
        return None
    url = next(
        (a["href"] for a in grid.find_all("a", href=True)
         if _RESULT_URL_RE.match(a["href"])),
        None,
    )
    if not url:
        return None
    m_vin = _VIN_FROM_URL_RE.search(url)
    vin   = m_vin.group(1).upper() if m_vin else ""
    price = IN_PROGRESS
    span  = grid.find("span", class_="prices")
    if span:
        raw = span.get_text(strip=True)
        if raw.isdigit():
            price = f"${int(raw):,}"
    return price, vin, url


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
        max_concurrent: int = 1,
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
        max_concurrent: int = 1,
    ) -> dict[str, tuple[str, str, str]]:
        if not queries:
            return {}
        return asyncio.run(
            self._lookup_many_async(queries, makes or {}, delay, max_concurrent)
        )

    def check_sale_ended_many(self, lot_urls: list[str]) -> dict[str, bool]:
        if not lot_urls:
            return {}
        return asyncio.run(self._sale_ended_many_async(lot_urls))

    def lookup(self, query: str, expected_make: str = "") -> tuple[str, str, str]:
        makes = {query: expected_make} if expected_make else None
        return self.lookup_many([query], makes=makes).get(query, (IN_PROGRESS, "", ""))

    def sale_ended(self, lot_url: str) -> bool:
        return self.check_sale_ended_many([lot_url]).get(lot_url, False)

    # ---- Async internals ---------------------------------------------------

    async def _start_browser(self):
        if self.browser_port:
            return await uc.start(host="127.0.0.1", port=self.browser_port)
        return await uc.start(
            headless=False, sandbox=False,
            browser_args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

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
        max_concurrent: int = 1,
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
            async with sem:
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

    async def _sale_ended_many_async(self, lot_urls: list[str]) -> dict[str, bool]:
        browser = await self._start_browser()
        try:
            sem = asyncio.Semaphore(_MAX_CONCURRENT)
            async def _one(url: str) -> tuple[str, bool]:
                async with sem:
                    tab = await browser.get(url, new_tab=True)
                    await asyncio.sleep(_COPART_RENDER_WAIT)
                    ended = SALE_ENDED_TEXT in await tab.get_content()
                    await tab.close()
                    return url, ended
            pairs = await asyncio.gather(*(_one(u) for u in lot_urls))
            return dict(pairs)
        finally:
            await self._stop_browser(browser)


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
        max_concurrent: int = 1,
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
    max_concurrent: int = 1,
) -> dict[str, tuple]:
    """Search bidfax for each query, using disk cache to skip known results.

    Only final (non-"In Progress") prices are cached.  `max_concurrent` > 1
    fans out across that many tabs (experimental, see BidfaxClient.lookup_many).
    """
    if client is None and not _DEPS_OK:
        print("[warn] nodriver/bs4 not installed — skipping bidfax lookups.")
        return {q: (IN_PROGRESS, "", "") for q in queries}

    cache    = load_cache(cache_path)
    to_fetch = [q for q in queries if q not in cache]

    if to_fetch:
        print(f"[*] bidfax lookup: {len(to_fetch)} new  (cached: {len(cache)})")
        real_client = client or BrowserBidfaxClient(browser_port=browser_port)
        fetched     = real_client.lookup_many(
            to_fetch, makes=makes, delay=delay, max_concurrent=max_concurrent,
        )
        cache.update({q: v for q, v in fetched.items() if v[0] != IN_PROGRESS})
        save_cache(cache_path, cache)

    return {q: cache.get(q, (IN_PROGRESS, "", "")) for q in queries}


def run_batch_vins(
    vins: list[str],
    delay: float,
    cache_path: Path,
    browser_port: int | None = None,
    client: BidfaxClient | None = None,
    max_concurrent: int = 1,
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


async def _fill_and_submit(page, query: str) -> bool:
    search_input = await page.find("#search")
    if not search_input:
        return False
    await asyncio.sleep(2)
    await search_input.click()
    await asyncio.sleep(0.5)
    await search_input.send_keys(query)
    await asyncio.sleep(0.5)
    submit_btn = await page.find("#submit")
    if not submit_btn:
        return False
    await submit_btn.click()
    return True


async def _wait_for_navigation(page) -> bool:
    for _ in range(10):
        await asyncio.sleep(1)
        try:
            current_url = page.url
        except Exception:
            current_url = ""
        if current_url and not _BIDFAX_HOME_PATH.match(current_url):
            return True
    return False


async def _search_once(page, query: str) -> tuple[str, str, str]:
    """Perform one bidfax search on an existing page. Returns (price, vin, url)."""
    if not await _fill_and_submit(page, query):
        return IN_PROGRESS, "", ""
    if not await _wait_for_navigation(page):
        return IN_PROGRESS, "", ""
    for i in range(15):
        await asyncio.sleep(1)
        html = await page.get_content()
        if "cf_chl" in html:
            continue
        result = extract_grid_result(html)
        if result is not None:
            return result
        if i >= 5:
            return IN_PROGRESS, "", ""
    return IN_PROGRESS, "", ""


async def _query_with_retries(page, query: str, expected_make: str) -> tuple[str, str, str]:
    """Run one bidfax search with up to 3 retries when the URL's make doesn't
    match `expected_make` (guards against bidfax returning a wrong vehicle —
    e.g. asking for Audi Q5 lot 41613606 but bidfax's top hit is a Nissan
    Leaf with the same digits somewhere in its listing).

    If every retry comes back with a mismatched make, we treat the lot as
    not-found and return (IN_PROGRESS, "", "") rather than hand back data
    that belongs to a different vehicle. When `expected_make` is empty
    (e.g. VIN lookups), the first URL is accepted without validation.
    """
    for _ in range(3):
        await page.get(BIDFAX_HOME)
        await asyncio.sleep(2)
        await _wait_cf_clear(page)
        price, vin, url = await _search_once(page, query)
        if not url:
            # bidfax returned no result — truly not found, surface that.
            return IN_PROGRESS, "", ""
        if not expected_make or url_make_matches(expected_make, url):
            return price, vin, url
        print(f"    [bidfax] make mismatch for {query!r}: "
              f"expected {expected_make!r}, got URL {url}", flush=True)
    # All retries returned URLs with the wrong make — refuse to use them.
    return IN_PROGRESS, "", ""
