#!/usr/bin/env python3
"""
Copart lot scraper — API probe variant.

Drop-in alternative to `copart_search.py`: same input CSV, same output CSV
columns. Different *how*:

  1. Playwright launches a real Chromium and visits copart.com (homepage)
     and /lotSearchResults/ so Copart's bot-defense session establishes —
     reese84 / incap_ses_* / XSRF-TOKEN end up in the cookie jar.
  2. Cookies are snapshotted via `context.cookies()` and Chromium is
     closed. This is the only place a browser is involved.
  3. A plain `requests.Session` carrying those cookies + a realistic
     header set is used for the actual paged JSON POSTs.

Why Playwright? nodriver's `cookies.get_all()` and `Storage.getCookies`
both hang on Copart (the page is responsive, JS `document.cookie` works,
but the cookie-API CDP path wedges — confirmed library bug). Playwright's
`context.cookies()` returns instantly with the same data.

Usage:
    python scripts/copart_search_api.py \
        --input filters/copart_filters.csv \
        --output copart_search_api_YYYY_MM_DD.csv
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clients import copart as copart_client
from core.csv_io import save_csv_dict
from scripts.copart_search import (
    process_filters,
    read_filters_csv,
)

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


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


COPART_HOME    = "https://www.copart.com"
SEARCH_API_URL = copart_client.SEARCH_API_URL
PAGE_SIZE      = copart_client.PAGE_SIZE
# After the homepage, hop to lotSearchResults so Copart's SPA fires the
# XSRF-TOKEN-init XHR. The existing HttpCopartClient does the same.
WARMUP_URLS = (COPART_HOME, f"{COPART_HOME}/lotSearchResults/")


# ---------------------------------------------------------------------------
# Payload — same shape as the captured browser request
# ---------------------------------------------------------------------------

def build_extended_payload(filters: dict, page: int = 0) -> dict:
    """Augment `build_search_payload` with the extra fields the real
    browser sends (VEHT, sort, includeTagByField, rawParams, etc.).
    Doesn't change which lots come back; makes the body indistinguishable
    from what the SPA emits."""
    payload = copart_client.build_search_payload(filters, page=page)
    payload.setdefault("filter", {})["VEHT"] = ["vehicle_type_code:VEHTYPE_V"]
    payload["sort"] = [
        "salelight_priority asc",
        "member_damage_group_priority asc",
        "auction_date_type desc",
        "auction_date_utc asc",
    ]
    payload["hideImages"]          = False
    payload["defaultSort"]         = False
    payload["specificRowProvided"] = False
    payload["displayName"]         = ""
    payload["backUrl"]             = ""
    payload["includeTagByField"]   = {key: f"{{!tag={key}}}"
                                      for key in payload["filter"]}
    payload["rawParams"]           = {}
    return payload


# ---------------------------------------------------------------------------
# Browser warmup — Playwright, one-shot
# ---------------------------------------------------------------------------

def warmup_cookies(headless: bool = False,
                   dwell_seconds: float = 4.0) -> tuple[list[dict], str | None]:
    """Launch Chromium → walk WARMUP_URLS → snapshot cookies → close.

    Returns (cookies, xsrf_token). Each cookie is the dict Playwright
    returns (`name`, `value`, `domain`, `path`, …), already shaped for
    feeding into requests.Session.cookies.
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
                # Dwell so any post-load XHR setting cookies (XSRF init,
                # bot-defense follow-ups) has a chance to land.
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
# CopartClient that uses warmed-up browser cookies
# ---------------------------------------------------------------------------

def build_session(cookies: list[dict], xsrf: str | None) -> "requests.Session":
    """Build a `requests.Session` ready to POST to Copart's search API:
    realistic browser headers + the cookies/XSRF-TOKEN snapshot from
    Playwright. Pure factory — no side effects."""
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
        # Spring CSRF: cookie value is echoed in the X-XSRF-TOKEN
        # request header. Missing it can produce a 403 with a JSON body.
        s.headers["X-XSRF-TOKEN"] = xsrf
    for c in cookies:
        # Playwright cookies use dict keys: name, value, domain, path.
        s.cookies.set(
            c["name"], c["value"],
            domain=c.get("domain") or None,
            path=c.get("path") or "/",
        )
    return s


class PlaywrightWarmupCopartClient:
    """Conforms to `copart_client.CopartClient`.

    Takes a pre-built `requests.Session` (carrying Playwright-captured
    cookies + a realistic header set) and uses it for paged JSON POSTs.
    No browser involvement here — warmup happens once in `main()`."""

    def __init__(self, session: "requests.Session", request_delay: float = 2.0) -> None:
        if not _REQUESTS_OK:
            raise RuntimeError("requests is required.  pip install requests")
        self._session      = session
        self.request_delay = request_delay

    def fetch_lots(self, filters: dict) -> list[dict]:
        session  = self._session
        all_lots: list[dict] = []
        page     = 0

        while True:
            payload = build_extended_payload(filters, page=page)
            try:
                resp = session.post(SEARCH_API_URL, json=payload, timeout=30)
                log.info(f"  Response: HTTP {resp.status_code} | "
                         f"size={len(resp.content)} bytes")
                resp.raise_for_status()
                data = resp.json()
            except requests.HTTPError as e:
                log.error(f"  HTTP {e.response.status_code}: {e.response.text[:600]}")
                break
            except requests.RequestException as e:
                log.error(f"  Request error: {e}")
                break
            except ValueError as e:
                log.error(f"  Bad JSON response: {e}")
                break

            results_data   = (data.get("data") or {}).get("results") or {}
            content        = results_data.get("content", [])
            total_elements = results_data.get("totalElements", 0)

            if not content:
                break
            all_lots.extend(content)
            log.info(f"  Got {len(content)} lots (total: {total_elements})")

            if len(all_lots) >= total_elements or len(content) < PAGE_SIZE:
                break
            page += 1
            time.sleep(self.request_delay)

        return all_lots


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    from datetime import date
    today = date.today().strftime("%Y_%m_%d")

    parser = argparse.ArgumentParser(
        description="Copart lot scraper — Playwright-warmed cookies + requests for the API hits.",
    )
    parser.add_argument("--input",  "-i", default="copart_filters.csv",
                        help="Input CSV with filter rows (default: copart_filters.csv)")
    parser.add_argument("--output", "-o", default=f"copart_search_api_{today}.csv",
                        help=f"Output CSV file (default: copart_search_api_{today}.csv)")
    parser.add_argument("--delay",  "-d", type=float, default=2.0,
                        help="Delay between paged API requests (s, default: 2.0)")
    parser.add_argument("--dwell-seconds", type=float, default=4.0,
                        help="Seconds to dwell on each warmup URL so cookies "
                             "land (default: 4.0)")
    parser.add_argument("--headless", action="store_true",
                        help="Run Chromium headless during warmup")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        log.error(f"Input file not found: {input_path}")
        return

    filters_list = read_filters_csv(str(input_path))
    log.info(f"Loaded {len(filters_list)} filter row(s) from {input_path}")
    if not filters_list:
        log.error("No filter rows found. Check your CSV format.")
        return

    log.info("Warming up Chromium to collect bot-defense cookies...")
    try:
        cookies, xsrf = warmup_cookies(
            headless=args.headless,
            dwell_seconds=args.dwell_seconds,
        )
    except Exception:
        import traceback
        log.error("Warmup failed:\n" + traceback.format_exc())
        return
    if not cookies:
        log.error("Warmup returned no cookies — bailing out.")
        return

    session  = build_session(cookies, xsrf)
    client   = PlaywrightWarmupCopartClient(session=session, request_delay=args.delay)
    all_rows = []

    for idx, filters in enumerate(filters_list, 1):
        log.info(f"[{idx}/{len(filters_list)}] Processing filter row...")
        try:
            rows = process_filters(filters, client)
            all_rows.extend(rows)
        except Exception:
            import traceback
            log.error("  Error:\n" + traceback.format_exc())
        time.sleep(args.delay)

    output_path = Path(args.output)
    fieldnames  = [
        "Make", "Model", "Year", "Odometer", "Fuel Type",
        "Lot Number", "Link", "Auction Date", "Location", "Primary Damage",
    ]
    save_csv_dict(output_path, fieldnames, all_rows)

    if all_rows:
        log.info(f"+ Saved {len(all_rows)} result(s) to {output_path}")
    else:
        log.warning(f"No matching lots found. Empty file written to {output_path}")


if __name__ == "__main__":
    main()
