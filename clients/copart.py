#!/usr/bin/env python3
"""
CopartClient abstraction — wraps HTTP access to Copart's internal search API.

  - CopartClient      — the interface scripts depend on
  - HttpCopartClient  — real implementation using requests
  - FakeCopartClient  — test double that returns canned lot lists

Copart has no browser requirement (pure HTTP/JSON), so there is no context-
manager lifecycle — clients are cheap to instantiate.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol, runtime_checkable

try:
    import requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL       = "https://www.copart.com"
SEARCH_API_URL = f"{BASE_URL}/public/lots/search-results"
PAGE_SIZE      = 100

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Content-Type":    "application/json",
    "Origin":          BASE_URL,
    "Referer":         f"{BASE_URL}/lotSearchResults/",
}

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Payload builder (pure — no HTTP)
# ---------------------------------------------------------------------------

def build_search_payload(filters: dict, page: int = 0) -> dict:
    make   = filters.get("make", "")
    models = filters.get("models") or []

    api_filter: dict = {}
    api_filter["FETI"] = ["lot_condition_code:CERT-D"]  # Run and Drive

    year_min = filters.get("year_min")
    year_max = filters.get("year_max") or (datetime.now(tz=timezone.utc).year + 1)
    y_from   = year_min if year_min else "*"
    api_filter["YEAR"] = [f"lot_year:[{y_from} TO {year_max}]"]

    if make:
        api_filter["MAKE"] = [f'lot_make_desc:"{make}"']
    if models:
        api_filter["MODL"] = [f'lot_model_desc:"{m}"' for m in models]

    fuel = filters.get("fuel_type")
    if fuel:
        api_filter["FUEL"] = [f'fuel_type_desc:"{fuel}"']

    odometer_max = filters.get("odometer_max")
    if odometer_max is not None:
        api_filter["ODM"] = [f"odometer_reading_received:[0 TO {odometer_max}]"]

    # Auction today or tomorrow (48h window, matches Copart's own default)
    now         = datetime.now(tz=timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_window  = today_start + timedelta(hours=47, minutes=59, seconds=59)
    api_filter["SDAT"] = [
        f'auction_date_utc:["{today_start.strftime("%Y-%m-%dT%H:%M:%SZ")}" '
        f'TO "{end_window.strftime("%Y-%m-%dT%H:%M:%SZ")}"]'
    ]

    return {
        "query":          ["*"],
        "filter":         api_filter,
        "sort":           None,
        "page":           page,
        "size":           PAGE_SIZE,
        "start":          page * PAGE_SIZE,
        "watchListOnly":  False,
        "freeFormSearch": False,
        "searchName":     "",
    }


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class CopartClient(Protocol):
    """Everything scripts need from copart.com."""

    def fetch_lots(self, filters: dict) -> list[dict]:
        """Run one filter search. Returns the raw Copart lot dicts (all pages)."""


# ---------------------------------------------------------------------------
# Real (HTTP-backed) implementation
# ---------------------------------------------------------------------------

class HttpCopartClient:
    """Live copart.com client — walks all result pages via the internal JSON API."""

    def __init__(self, request_delay: float = 2.0) -> None:
        if not _REQUESTS_OK:
            raise RuntimeError("requests is required. Install with:  pip install requests")
        self.request_delay = request_delay
        self._session: "requests.Session | None" = None

    def _session_ready(self) -> "requests.Session":
        if self._session is not None:
            return self._session
        s = requests.Session()
        s.headers.update(HEADERS)
        for url in (BASE_URL, f"{BASE_URL}/lotSearchResults/"):
            try:
                r = s.get(url, timeout=15)
                log.info(f"Warmup {url}: HTTP {r.status_code} | cookies: {list(s.cookies.keys())}")
            except Exception as e:
                log.warning(f"Warmup {url} failed: {e}")
        self._session = s
        return s

    def fetch_lots(self, filters: dict) -> list[dict]:
        session  = self._session_ready()
        all_lots: list[dict] = []
        page     = 0

        while True:
            payload = build_search_payload(filters, page=page)
            try:
                resp = session.post(SEARCH_API_URL, json=payload, timeout=30)
                log.info(f"  Response: HTTP {resp.status_code} | size={len(resp.content)} bytes")
                resp.raise_for_status()
                data = resp.json()
            except requests.HTTPError as e:
                log.error(f"  HTTP {e.response.status_code}: {e.response.text[:600]}")
                break
            except Exception as e:
                log.error(f"  Request error: {e}")
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
# Fake (in-memory) implementation for tests
# ---------------------------------------------------------------------------

class FakeCopartClient:
    """In-memory CopartClient.

    Two modes:
      - flat:      FakeCopartClient(lots=[...])            returns same list for every filter
      - callable:  FakeCopartClient(fetch_fn=lambda f: ...) compute per-filter result
    """

    def __init__(
        self,
        lots: list[dict] | None = None,
        fetch_fn: Callable[[dict], list[dict]] | None = None,
    ) -> None:
        if lots is None and fetch_fn is None:
            lots = []
        self._lots     = list(lots or [])
        self._fetch_fn = fetch_fn
        self.calls: list[dict] = []

    def fetch_lots(self, filters: dict) -> list[dict]:
        self.calls.append(dict(filters))
        if self._fetch_fn is not None:
            return list(self._fetch_fn(filters))
        return list(self._lots)
