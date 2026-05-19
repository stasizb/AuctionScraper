"""Shared Playwright-warmed `requests.Session` for IAAI API calls.

Independent of `clients/copart_session.py` — own cookie cache, own
warmup URL, no cross-talk. Both modules follow the same shape so the
mental model carries over, but they never share state.

IAAI's public /Search endpoint sits behind a bot-defense session
(Cloudflare-ish heuristics + the usual fingerprinting). Plain `requests`
warmups don't reliably get past it; a real Chromium does. Playwright
launches once, navigates to /Search so the defense issues its session
cookies, and we copy the cookies into a plain `requests.Session` for
all the actual API work that follows.

This module exists so callers (scripts/iaai_search.py today, possibly
others later) can share one warmup via the on-disk cookie cache.
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


IAAI_HOME = "https://www.iaai.com"
# One nav: /Search lands the SPA, which establishes the bot-defense
# cookies + (often) a CSRF/anti-forgery cookie. Two-hop warmup like
# Copart's is unnecessary here — the same page that initialises the
# search SPA is the one we'll POST to.
WARMUP_URL = f"{IAAI_HOME}/Search"

DEFAULT_CACHE_PATH = Path("caches/iaai_cookies.json")
# Match the Copart cache TTL — long enough to cover a typical daily-run
# window, short enough that an overnight stale cache forces re-warmup.
DEFAULT_CACHE_TTL_SECONDS = 1800  # 30 min


# ---------------------------------------------------------------------------
# Browser warmup
# ---------------------------------------------------------------------------

def warmup_cookies(
    headless: bool = False,
    dwell_seconds: float = 5.0,
) -> tuple[list[dict], str]:
    """Launch Chromium → visit /Search → snapshot cookies + UA → close.

    Returns (cookies, user_agent). `cookies` is a list of Playwright
    cookie dicts (`name`, `value`, `domain`, `path`, ...) ready to feed
    into `requests.Session.cookies.set(**c)`. `user_agent` is the UA
    string the warmed browser used — passing it through to `requests`
    keeps the session fingerprint consistent.

    `headless=False` is the default: headless Chromium tends to capture
    a thinner cookie set under bot-defense fingerprinting. A briefly-
    visible Chromium window during warmup is the price of reliability.
    """
    if not _PLAYWRIGHT_OK:
        raise RuntimeError("playwright is required. Install with:  "
                           "pip install playwright && python -m playwright install chromium")

    cookies: list[dict] = []
    user_agent: str = ""

    with sync_playwright() as p:
        log.info("  [warmup] launching Chromium...")
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context()
            page    = context.new_page()
            log.info(f"  [warmup] GET {WARMUP_URL}")
            try:
                page.goto(WARMUP_URL, wait_until="domcontentloaded",
                          timeout=30_000)
            except Exception as e:
                log.info(f"  [warmup] nav warning: {e!r}")
            page.wait_for_timeout(int(dwell_seconds * 1000))
            cookies = context.cookies()
            try:
                user_agent = page.evaluate("navigator.userAgent") or ""
            except Exception:
                user_agent = ""
            log.info(f"  [warmup] captured {len(cookies)} cookie(s)")
        finally:
            log.info("  [warmup] closing Chromium")
            browser.close()

    return cookies, user_agent


# ---------------------------------------------------------------------------
# Session builder
# ---------------------------------------------------------------------------

# Headers IAAI's own SPA sends. Replicated so our requests look identical.
def _default_headers(user_agent: str) -> dict[str, str]:
    return {
        "User-Agent":      user_agent or
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/147.0.0.0 Safari/537.36",
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type":    "application/json",
        "Origin":          IAAI_HOME,
        "Referer":         f"{IAAI_HOME}/Search",
        "X-Requested-With": "XMLHttpRequest",
    }


def build_session(cookies: list[dict],
                  user_agent: str = "") -> "requests.Session":
    """Build a `requests.Session` ready to POST to IAAI's public /Search:
    browser-like headers + the cookies snapshot. Pure factory — no I/O."""
    if not _REQUESTS_OK:
        raise RuntimeError("requests is required.  pip install requests")
    s = requests.Session()
    s.headers.update(_default_headers(user_agent))
    for c in cookies:
        s.cookies.set(c["name"], c["value"],
                      domain=c.get("domain") or None,
                      path=c.get("path") or "/")
    return s


# ---------------------------------------------------------------------------
# On-disk cookie cache (so multiple scripts in one daily run share warmup)
# ---------------------------------------------------------------------------

def save_cookies(cache_path: Path,
                 cookies: list[dict],
                 user_agent: str) -> None:
    """Persist cookies + UA to `cache_path` as JSON. Best-effort: errors
    are logged, not raised."""
    try:
        cache_path = Path(cache_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({
            "timestamp":  time.time(),
            "cookies":    cookies,
            "user_agent": user_agent,
        }), encoding="utf-8")
    except OSError as e:
        log.warning(f"could not write cookie cache to {cache_path}: {e!r}")


def load_cookies(
    cache_path: Path,
    max_age_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
) -> tuple[list[dict], str] | None:
    """Read cookies + UA from `cache_path` if it exists and is younger
    than `max_age_seconds`. Returns None on miss, stale, or parse error
    (so the caller will re-warm)."""
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
    return cookies, blob.get("user_agent") or ""


def get_or_warmup_session(
    cache_path: Path = DEFAULT_CACHE_PATH,
    max_age_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    headless: bool = False,
    dwell_seconds: float = 5.0,
) -> "requests.Session":
    """Return a `requests.Session` ready for IAAI API calls.

    Uses cached cookies from `cache_path` when fresh; otherwise runs the
    Playwright warmup once and writes the cache for the next caller.
    This is the single entry point all IAAI-API consumers should use.
    """
    cached = load_cookies(cache_path, max_age_seconds)
    if cached is not None:
        cookies, ua = cached
        try:
            age = int(time.time() -
                      json.loads(Path(cache_path).read_text())["timestamp"])
        except Exception:
            age = -1
        log.info(f"[iaai-session] using cached cookies from {cache_path} "
                 f"({len(cookies)} cookie(s), age {age}s)")
        return build_session(cookies, ua)

    log.info(f"[iaai-session] no fresh cache at {cache_path} — warming up")
    cookies, ua = warmup_cookies(headless=headless,
                                 dwell_seconds=dwell_seconds)
    save_cookies(cache_path, cookies, ua)
    return build_session(cookies, ua)
