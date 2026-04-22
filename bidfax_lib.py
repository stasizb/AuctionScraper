#!/usr/bin/env python3
"""
Shared bidfax.info search library.

Used by bidfax_search.py (lot-based search) and any other script that
needs to look up data on bidfax.info via a real browser session.

Public API:
    run_batch(queries, delay, cache_path)  — batch search with disk cache
    load_cache(path) / save_cache(path, cache)  — cache helpers

Each cache entry is keyed by the search query (lot number or VIN) and
stores a tuple  (price, vin, bidfax_url):
    price       "$X,XXX"  if final, "In Progress" if not yet available
    vin         VIN string extracted from the result URL, or ""
    bidfax_url  full bidfax.info URL, or ""

Only results with a confirmed final price are persisted — "In Progress"
entries are retried on the next run.
"""

import asyncio
import json
import re
from pathlib import Path

try:
    import nodriver as uc
    from bs4 import BeautifulSoup
    _DEPS_OK = True
except ImportError:
    _DEPS_OK = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BIDFAX_HOME       = "https://bidfax.info"
_IN_PROGRESS      = "In Progress"
_CF_WAIT_TIMEOUT  = 30.0
_BIDFAX_HOME_PATH = re.compile(r'^https?://bidfax\.info/?$')
_RESULT_URL_RE    = re.compile(r'^https://bidfax\.info/[^/]+/[^/]+/.+\.html$')
_VIN_FROM_URL_RE  = re.compile(r'-vin-([a-z0-9]+)\.html$', re.IGNORECASE)

# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def load_cache(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # Normalise: values must be [price, vin, url] lists (JSON arrays)
            return {k: tuple(v) if isinstance(v, list) else v for k, v in data.items()}
        except ValueError:
            return {}
    return {}


def save_cache(path: Path, cache: dict) -> None:
    # Store tuples as lists (JSON-serialisable)
    serialisable = {k: list(v) if isinstance(v, tuple) else v for k, v in cache.items()}
    path.write_text(json.dumps(serialisable, indent=2, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

async def _wait_cf_clear(page) -> None:
    """Poll until Cloudflare challenge is gone (max _CF_WAIT_TIMEOUT seconds)."""
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
    """Type *query* into the bidfax search form and submit it.

    Returns False if the form elements are not found.
    Includes an extra wait after finding the input so form JS is fully ready.
    """
    search_input = await page.find("#search")
    if not search_input:
        return False

    await asyncio.sleep(2)          # let form JS fully initialise
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
    """Wait until the browser leaves the bidfax home page URL.

    Returns False if the URL has not changed after 10 seconds, meaning the
    form submission did not navigate away (e.g. the page was not ready).
    """
    for _ in range(10):
        await asyncio.sleep(1)
        try:
            current_url = page.url
        except Exception:
            current_url = ""
        if current_url and not _BIDFAX_HOME_PATH.match(current_url):
            return True
    return False


def _extract_grid_result(html: str) -> tuple[str, str, str] | None:
    """Parse the results page HTML and extract (price, vin, url) from #grid.

    Returns None when the grid is absent or contains no matching result links,
    so the caller can keep polling until the page finishes loading.
    """
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

    price = _IN_PROGRESS
    span  = grid.find("span", class_="prices")
    if span:
        raw = span.get_text(strip=True)
        if raw.isdigit():
            price = f"${int(raw):,}"

    return price, vin, url


async def search_bidfax(page, query: str) -> tuple[str, str, str]:
    """Search bidfax.info for *query* (lot number or VIN) using an open page.

    Returns (price, vin, bidfax_url):
      - price      "$X,XXX" if final, "In Progress" otherwise
      - vin        extracted from result URL, or ""
      - bidfax_url first matching result URL, or ""

    The caller is responsible for navigating to the home page before calling
    this function (so the form is clean for each search).
    """
    if not await _fill_and_submit(page, query):
        return _IN_PROGRESS, "", ""

    if not await _wait_for_navigation(page):
        return _IN_PROGRESS, "", ""

    for i in range(15):
        await asyncio.sleep(1)
        html = await page.get_content()
        if "cf_chl" in html:
            continue
        result = _extract_grid_result(html)
        if result is not None:
            return result
        if i >= 5:
            return _IN_PROGRESS, "", ""

    return _IN_PROGRESS, "", ""


# ---------------------------------------------------------------------------
# Make validation
# ---------------------------------------------------------------------------

def url_make_matches(csv_make: str, bidfax_url: str) -> bool:
    """Return True if the make in the bidfax URL matches the expected CSV make.

    bidfax URLs follow the pattern https://bidfax.info/<make>/<model>/...
    Both sides are normalised to lowercase with spaces converted to hyphens
    before comparison, so "MERCEDES-BENZ" matches "mercedes-benz".
    """
    parts    = bidfax_url.replace("https://bidfax.info/", "").split("/")
    url_make = parts[0].lower() if parts else ""
    norm     = re.sub(r"[\s_]+", "-", csv_make.strip().lower())
    return bool(url_make) and (url_make == norm
                                or norm.startswith(url_make)
                                or url_make.startswith(norm))


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

async def _run_batch_async(
    queries: list[str],
    delay: float,
    makes: dict[str, str] | None = None,
    browser_port: int | None = None,
) -> dict[str, tuple]:
    """Open one browser session and search every query. Returns {query: (price, vin, url)}.

    *makes* is an optional {lot: make} mapping used to validate that the
    returned bidfax URL belongs to the expected vehicle make.  When the URL
    make does not match, the result is retried up to 3 times before giving up.

    *browser_port* connects to an already-running Chrome instead of starting a new one.
    """
    if browser_port:
        browser = await uc.start(host="127.0.0.1", port=browser_port)
    else:
        browser = await uc.start(
            headless=False,
            sandbox=False,
            browser_args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
    results: dict[str, tuple] = {}
    try:
        page = await browser.get(BIDFAX_HOME)
        print("[*] Waiting for bidfax.info (Cloudflare)...")
        await _wait_cf_clear(page)
        print("[+] Ready.")

        for i, query in enumerate(queries, 1):
            print(f"  [{i}/{len(queries)}] {query!r} … ", end="", flush=True)
            expected_make = (makes or {}).get(query, "")

            for attempt in range(3):
                await page.get(BIDFAX_HOME)
                await asyncio.sleep(2)
                await _wait_cf_clear(page)
                price, vin, url = await search_bidfax(page, query)
                if url and expected_make and not url_make_matches(expected_make, url):
                    print(f"\n    [warn] URL make mismatch (attempt {attempt + 1}): {url}")
                    continue
                break

            results[query] = (price, vin, url)
            print(f"{price}  VIN:{vin or '—'}  {url or 'not found'}")
            if i < len(queries):
                await asyncio.sleep(delay)
    finally:
        try:
            await browser.stop()
        except Exception:
            pass
    return results


async def _run_batch_vins_async(
    vins: list[str],
    delay: float,
    browser_port: int | None = None,
) -> dict[str, str]:
    """Search bidfax.info for each VIN string. Returns {vin: url}."""
    if browser_port:
        browser = await uc.start(host="127.0.0.1", port=browser_port)
    else:
        browser = await uc.start(
            headless=False,
            sandbox=False,
            browser_args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
    results: dict[str, str] = {}
    try:
        page = await browser.get(BIDFAX_HOME)
        print("[*] Waiting for bidfax.info (Cloudflare)...")
        await _wait_cf_clear(page)
        print("[+] Ready.")
        for i, vin in enumerate(vins, 1):
            print(f"  [{i}/{len(vins)}] {vin} … ", end="", flush=True)
            await page.get(BIDFAX_HOME)
            await asyncio.sleep(2)
            await _wait_cf_clear(page)
            _, _, url = await search_bidfax(page, vin)
            results[vin] = url
            print(url or "not found")
            if i < len(vins):
                await asyncio.sleep(delay)
    finally:
        try:
            await browser.stop()
        except Exception:
            pass
    return results


def run_batch_vins(
    vins: list[str],
    delay: float,
    cache_path: Path,
    browser_port: int | None = None,
) -> dict[str, str]:
    """Search bidfax.info for each VIN, returning {vin: url}. Uses disk cache.

    Cache entries are stored as ("", vin, url) tuples alongside lot entries.
    Legacy plain-string entries written by older code are also recognised.
    Only found URLs are cached — not-found VINs are retried on the next run.
    """
    if not _DEPS_OK:
        print("[warn] bidfax_lib: nodriver or beautifulsoup4 not installed — skipping lookups.")
        return dict.fromkeys(vins, "")

    cache = load_cache(cache_path)

    def _cached_url(vin: str) -> str:
        entry = cache.get(vin)
        if isinstance(entry, (list, tuple)) and len(entry) >= 3:
            return entry[2]
        if isinstance(entry, str):
            return entry  # legacy format written by older workbook_to_html
        return ""

    to_fetch = [v for v in vins if not _cached_url(v)]

    if to_fetch:
        print(f"[*] bidfax VIN lookup: {len(to_fetch)} new  (cached: {len(vins) - len(to_fetch)})")
        fetched = asyncio.run(_run_batch_vins_async(to_fetch, delay, browser_port=browser_port))
        cache.update({v: ("", v, url) for v, url in fetched.items() if url})
        save_cache(cache_path, cache)

    return {v: _cached_url(v) for v in vins}


def run_batch(
    queries: list[str],
    delay: float,
    cache_path: Path,
    makes: dict[str, str] | None = None,
    browser_port: int | None = None,
) -> dict[str, tuple]:
    """Search bidfax.info for each query, using disk cache to skip known results.

    *makes* is an optional {lot: make} mapping forwarded to the browser session
    for URL make validation (guards against bidfax returning a wrong vehicle).

    Returns {query: (price, vin, bidfax_url)}.
    Only final prices are cached — "In Progress" results are retried next run.
    """
    if not _DEPS_OK:
        print("[warn] bidfax_lib: nodriver or beautifulsoup4 not installed — skipping lookups.")
        return {q: (_IN_PROGRESS, "", "") for q in queries}

    cache    = load_cache(cache_path)
    to_fetch = [q for q in queries if q not in cache]

    if to_fetch:
        print(f"[*] bidfax lookup: {len(to_fetch)} new  (cached: {len(cache)})")
        fetched = asyncio.run(_run_batch_async(to_fetch, delay, makes=makes, browser_port=browser_port))
        cache.update({q: v for q, v in fetched.items() if v[0] != _IN_PROGRESS})
        save_cache(cache_path, cache)

    return {q: cache.get(q, (_IN_PROGRESS, "", "")) for q in queries}
