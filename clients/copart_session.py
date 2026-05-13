"""Shared Playwright-warmed `requests.Session` for Copart API calls.

Copart's public endpoints sit behind Incapsula bot-defense. Plain
`requests` warmups don't reliably get past the JS challenge, but a real
Chromium does. Playwright launches once, navigates to copart.com so the
defense issues its session cookies, and we copy the cookies into a plain
`requests.Session` for all the actual API work that follows.

This module exists so multiple scripts in a single daily run can share
one warmup. The first caller runs the browser and writes cookies to a
small JSON cache; subsequent callers within the TTL window read the cache
and skip the browser entirely.

Two scripts depend on this:
  - scripts/copart_search.py        — daily search-results
  - scripts/bidfax_run.py (via      — Sale-Ended check via
    clients.copart.check_sale_ended_via_search)
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
    _PLAYWRIGHT_OK = True
except ImportError:
    _PLAYWRIGHT_OK = False

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False


log = logging.getLogger(__name__)


COPART_HOME = "https://www.copart.com"
# Two-hop warmup: homepage establishes Incapsula's reese84-style cookies,
# /lotSearchResults/ wakes the SPA so any Angular-init cookies (XSRF
# token in particular) land too.
WARMUP_URLS = (COPART_HOME, f"{COPART_HOME}/lotSearchResults/")

DEFAULT_CACHE_PATH = Path("caches/copart_cookies.json")
# How long a cached cookie set stays usable. Long enough to cover a
# typical daily-run window (copart_search → bidfax_run, often minutes
# apart), short enough that an overnight stale cache forces a re-warmup.
DEFAULT_CACHE_TTL_SECONDS = 1800  # 30 min


# ---------------------------------------------------------------------------
# Browser warmup
# ---------------------------------------------------------------------------

def warmup_cookies(
    headless: bool = False,
    dwell_seconds: float = 4.0,
) -> tuple[list[dict], str | None]:
    """Launch Chromium → walk `WARMUP_URLS` → snapshot cookies → close.

    Returns (cookies, xsrf_token).  Each cookie is the Playwright dict
    shape (`name`, `value`, `domain`, `path`, ...) so it plugs straight
    into `requests.Session.cookies.set(**c)`.

    `headless=False` is the default because headless Chromium gets a
    thinner cookie set (no Incapsula bot-defense cookies) and the
    downstream API hits 403. A briefly-visible Chromium window during
    warmup is the price of reliability.
    """
    if not _PLAYWRIGHT_OK:
        raise RuntimeError("playwright is required. Install with:  "
                           "pip install playwright && python -m playwright install chromium")

    cookies: list[dict] = []
    xsrf:    str | None = None

    with sync_playwright() as p:
        log.info("  [warmup] launching Chromium...")
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context()
            page    = context.new_page()
            for url in WARMUP_URLS:
                log.info(f"  [warmup] GET {url}")
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                except Exception as e:
                    # Continue even on partial load — cookies often land
                    # before DOMContentLoaded anyway.
                    log.info(f"  [warmup] nav warning: {e!r}")
                page.wait_for_timeout(int(dwell_seconds * 1000))

            cookies = context.cookies()
            for c in cookies:
                if c.get("name") == "XSRF-TOKEN":
                    xsrf = c.get("value")
            log.info(f"  [warmup] captured {len(cookies)} cookie(s); "
                     f"XSRF-TOKEN: {'present' if xsrf else 'missing'}")
        finally:
            log.info("  [warmup] closing Chromium")
            browser.close()

    return cookies, xsrf


# ---------------------------------------------------------------------------
# Session builder
# ---------------------------------------------------------------------------

def build_session(cookies: list[dict], xsrf: str | None) -> "requests.Session":
    """Build a `requests.Session` ready to POST to Copart's public API:
    realistic browser headers + the cookies/XSRF-TOKEN snapshot. Pure
    factory — no I/O."""
    if not _REQUESTS_OK:
        raise RuntimeError("requests is required.  pip install requests")
    s = requests.Session()
    s.headers.update({
        "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/147.0.0.0 Safari/537.36",
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Content-Type":    "application/json",
        "Origin":          COPART_HOME,
        "Referer":         f"{COPART_HOME}/lotSearchResults/",
        "X-Requested-With":   "XMLHttpRequest",
        "sec-ch-ua":          '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile":   "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest":  "empty",
        "sec-fetch-mode":  "cors",
        "sec-fetch-site":  "same-origin",
    })
    if xsrf:
        # Spring CSRF: cookie value is echoed in the X-XSRF-TOKEN header.
        s.headers["X-XSRF-TOKEN"] = xsrf
    for c in cookies:
        s.cookies.set(c["name"], c["value"],
                      domain=c.get("domain") or None,
                      path=c.get("path") or "/")
    return s


# ---------------------------------------------------------------------------
# On-disk cookie cache (so copart_search.py and bidfax_run.py share warmup)
# ---------------------------------------------------------------------------

def save_cookies(cache_path: Path, cookies: list[dict], xsrf: str | None) -> None:
    """Persist cookies + xsrf to `cache_path` as JSON. Creates parent
    directory if needed. Best-effort: errors are logged, not raised."""
    try:
        cache_path = Path(cache_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({
            "timestamp": time.time(),
            "cookies":   cookies,
            "xsrf":      xsrf,
        }), encoding="utf-8")
    except OSError as e:
        log.warning(f"could not write cookie cache to {cache_path}: {e!r}")


def load_cookies(
    cache_path: Path,
    max_age_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
) -> tuple[list[dict], str | None] | None:
    """Read cookies + xsrf from `cache_path` if the file exists and is
    younger than `max_age_seconds`. Returns None on miss, stale, or any
    parse error (so the caller will re-warm)."""
    try:
        raw = Path(cache_path).read_text(encoding="utf-8")
        blob = json.loads(raw)
    except (OSError, ValueError):
        return None
    ts = blob.get("timestamp")
    if not isinstance(ts, (int, float)) or (time.time() - ts) > max_age_seconds:
        return None
    cookies = blob.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        return None
    return cookies, blob.get("xsrf")


def get_or_warmup_session(
    cache_path: Path = DEFAULT_CACHE_PATH,
    max_age_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    headless: bool = False,
    dwell_seconds: float = 4.0,
) -> "requests.Session":
    """Return a `requests.Session` ready for Copart API calls.

    Uses cached cookies from `cache_path` when fresh; otherwise runs the
    Playwright warmup once and writes the cache for the next caller. This
    is the single entry point all Copart-API scripts should use so that
    multiple scripts in one daily run share a single Chromium launch.
    """
    cached = load_cookies(cache_path, max_age_seconds)
    if cached is not None:
        cookies, xsrf = cached
        age = int(time.time() - json.loads(Path(cache_path).read_text())["timestamp"])
        log.info(f"[copart-session] using cached cookies from {cache_path} "
                 f"({len(cookies)} cookie(s), age {age}s)")
        return build_session(cookies, xsrf)

    log.info(f"[copart-session] no fresh cache at {cache_path} — warming up")
    cookies, xsrf = warmup_cookies(headless=headless, dwell_seconds=dwell_seconds)
    save_cookies(cache_path, cookies, xsrf)
    return build_session(cookies, xsrf)
