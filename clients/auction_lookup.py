"""On-demand Copart and IAAI lookup by VIN or lot number."""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

from clients.copart import SEARCH_API_URL
from clients.copart_session import (
    CopartBlockedError,
    CopartCaptchaError,
    get_or_warmup_session as copart_session,
    search_in_browser as copart_browser_search,
)
from clients.iaai import IAAI_SEARCH_URL, parse_search_html
from clients.iaai_session import get_or_warmup_session as iaai_session


VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$", re.IGNORECASE)
LOT_RE = re.compile(r"^[0-9]{4,12}$")
_CACHE_DIR = Path(os.getenv("AUCTION_CACHE_DIR", "/tmp/auction-scraper"))


def normalize_query(value: str) -> tuple[str, str]:
    query = (value or "").strip().upper()
    if VIN_RE.fullmatch(query):
        return query, "vin"
    if LOT_RE.fullmatch(query):
        return query, "lot"
    raise ValueError("query must be a 17-character VIN or a 4-12 digit lot number")


def _copart_row(lot: dict) -> dict:
    number = str(lot.get("lotNumberStr") or lot.get("ln") or lot.get("lotNumber") or "")
    slug = lot.get("ldu") or ""
    link = f"https://www.copart.com/lot/{number}"
    if slug:
        link += f"/{slug}"
    return {
        "auction": "copart",
        "lot_number": number,
        "vin": lot.get("fv") or lot.get("vin") or lot.get("vinNumber") or "",
        "year": lot.get("lcy") or lot.get("lotYear") or "",
        "make": lot.get("mkn") or lot.get("make") or "",
        "model": lot.get("lm") or lot.get("model") or "",
        "odometer": lot.get("orr") or lot.get("od") or "",
        "location": lot.get("yn") or lot.get("yard") or "",
        "primary_damage": lot.get("dd") or "",
        "url": link,
    }


def _iaai_row(row: dict) -> dict:
    return {
        "auction": "iaai",
        "lot_number": row.get("Lot Number", ""),
        "vin": row.get("VIN", ""),
        "year": row.get("Year", ""),
        "make": row.get("Make", ""),
        "model": row.get("Model", ""),
        "odometer": row.get("Odometer", ""),
        "location": row.get("Location", ""),
        "primary_damage": row.get("Primary Damage", ""),
        "auction_date": row.get("Auction Date", ""),
        "acv": row.get("ACV", ""),
        "url": row.get("Link", ""),
    }


class AuctionLookup:
    """Lazily creates and reuses warmed HTTP sessions for both auctions."""

    def __init__(self, browser_search=None) -> None:
        self._sessions: dict[str, object] = {}
        self._locks = {"copart": threading.Lock(), "iaai": threading.Lock()}
        self._browser_search = browser_search or copart_browser_search

    def _session(self, auction: str):
        if auction in self._sessions:
            return self._sessions[auction]
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if auction == "copart":
            session = copart_session(
                cache_path=_CACHE_DIR / "copart_cookies.json", headless=True
            )
        else:
            session = iaai_session(
                cache_path=_CACHE_DIR / "iaai_cookies.json", headless=True
            )
        self._sessions[auction] = session
        return session

    def search_copart(self, query: str) -> list[dict]:
        with self._locks["copart"]:
            payload = {
                "query": [query], "filter": {}, "page": 0, "size": 100,
                "start": 0, "watchListOnly": False, "freeFormSearch": True,
            }
            try:
                session = self._session("copart")
            except Exception:
                return self._search_copart_in_browser(query)
            response = session.post(SEARCH_API_URL, json=payload, timeout=30)
            if response.status_code == 403:
                # A requests.Session can still be rejected when the Railway IP
                # or its HTTP fingerprint is blocked. Retry once inside the
                # warmed Chromium page, where headers and cookies stay aligned.
                return self._search_copart_in_browser(query)
            response.raise_for_status()
            content = (((response.json().get("data") or {}).get("results") or {})
                       .get("content", []))
            return [_copart_row(lot) for lot in content]

    def _search_copart_in_browser(self, query: str) -> list[dict]:
        try:
            lots = self._browser_search(query, headless=True)
        except (CopartCaptchaError, CopartBlockedError):
            raise
        except Exception as exc:
            raise CopartBlockedError("Copart Chromium fallback is unavailable") from exc
        return [_copart_row(lot) for lot in lots]

    def search_iaai(self, query: str) -> list[dict]:
        with self._locks["iaai"]:
            payload = {
                "Searches": [{"Facets": None, "FullSearch": query, "LongRanges": None}],
                "ZipCode": "", "miles": 0, "PageSize": 100, "CurrentPage": 1,
                "Sort": [{"IsGeoSort": False, "SortField": "TenantSortOrder",
                          "IsDescending": False}],
                "ShowRecommendations": False, "SaleStatusFilters": [],
                "BidStatusFilters": [],
            }
            url = f"{IAAI_SEARCH_URL}?c={int(time.time() * 1000)}"
            response = self._session("iaai").post(
                url, json=payload, timeout=30, allow_redirects=False
            )
            response.raise_for_status()
            return [_iaai_row(row) for row in parse_search_html(response.text)]

    def search(self, query: str, auction: str = "all") -> dict:
        query, query_type = normalize_query(query)
        if auction not in {"all", "copart", "iaai"}:
            raise ValueError("auction must be one of: all, copart, iaai")

        sources = ("copart", "iaai") if auction == "all" else (auction,)
        results: list[dict] = []
        errors: dict[str, str] = {}
        for source in sources:
            try:
                results.extend(getattr(self, f"search_{source}")(query))
            except (CopartCaptchaError, CopartBlockedError) as exc:
                errors[source] = str(exc)
                self._sessions.pop(source, None)
            except Exception as exc:
                # Do not echo response bodies, request headers, cookies, or
                # credentials from arbitrary client exceptions.
                errors[source] = f"{source.upper()} source unavailable ({type(exc).__name__})"
                self._sessions.pop(source, None)
        return {
            "query": query,
            "query_type": query_type,
            "count": len(results),
            "results": results,
            "errors": errors,
        }
