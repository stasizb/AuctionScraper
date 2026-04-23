#!/usr/bin/env python3
"""
IAAIClient abstraction — wraps all browser interaction with iaai.com.

  - IAAIClient         — the interface scripts depend on
  - BrowserIAAIClient  — real implementation using nodriver
  - FakeIAAIClient     — test double that returns canned row lists

Filter parsing (read_filters_csv / parse_filter_row) and the equipment
post-filter (equipment_matches) are pure helpers also exposed from this
module so the CLI wrapper stays thin.
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
import socket
import sys
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.chrome import find_chrome
from core.dates  import normalize_auction_date

try:
    import nodriver as uc
    _NODRIVER_OK = True
except ImportError:
    _NODRIVER_OK = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IAAI_SEARCH_URL = "https://www.iaai.com/Search"

AUCTION_DATE_COL = "Auction Date"

OUTPUT_FIELDS = [
    "Make", "Model", "Year", "Odometer", "Fuel Type",
    "Lot Number", "Link", AUCTION_DATE_COL, "Location",
    "Primary Damage", "ACV",
]

WAIT_SHORT  = 1.5
WAIT_MEDIUM = 3.0
WAIT_LONG   = 5.0


# ---------------------------------------------------------------------------
# Pure helpers (no browser)
# ---------------------------------------------------------------------------

def equipment_matches(full_title: str, equipment: str) -> bool:
    """Return True when every word in `equipment` appears as a token in `full_title`."""
    if not equipment:
        return True
    title_tokens   = set(re.findall(r'\S+', full_title.upper()))
    required_words = [w.upper() for w in equipment.split() if w.strip()]
    return all(word in title_tokens for word in required_words)


def _reassemble_segments(raw_line: str) -> list[str]:
    segments: list[str] = []
    buffer = ""
    for part in raw_line.split(","):
        if ":" in part:
            if buffer:
                segments.append(buffer.strip())
            buffer = part
        else:
            buffer = (buffer + ", " + part) if buffer else part
    if buffer:
        segments.append(buffer.strip())
    return segments


def _apply_segment(filters: dict, key: str, val: str) -> None:
    if key == "make":
        filters["make"] = val.upper()
    elif key == "model":
        models = [v.strip().upper() for v in val.split(";") if v.strip()]
        filters["models"] = models if models else [val.upper()]
    elif key in ("year_min", "yearmin"):
        try: filters["year_min"] = int(val)
        except ValueError: pass
    elif key in ("year_max", "yearmax"):
        try: filters["year_max"] = int(val)
        except ValueError: pass
    elif key in ("odometer_max", "odo_max", "odometer"):
        try: filters["odometer_max"] = int(val)
        except ValueError: pass
    elif key in ("fuel_type", "fueltype", "fuel"):
        filters["fuel_type"] = val.strip()
    elif key == "equipment":
        filters["equipment"] = val.strip()


def parse_filter_row(raw_line: str) -> dict:
    """Parse one line of 'Key: Value' comma-separated segments."""
    filters: dict = {}
    for seg in _reassemble_segments(raw_line):
        m = re.match(r"^([^:]+?)\s*:\s*(.+)$", seg.strip())
        if not m:
            continue
        key = m.group(1).strip().lower().replace(" ", "_")
        _apply_segment(filters, key, m.group(2).strip())
    return filters


def read_filters_csv(path: str) -> list[dict]:
    filter_list: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parsed = parse_filter_row(line)
            if parsed:
                filter_list.append(parsed)
    return filter_list


def write_output_csv(path: str, records: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    print(f"[+] Saved {len(records)} record(s) -> {path}")


def _unwrap_evaluate_result(raw):
    for _ in range(5):
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict) and "value" in raw:
            raw = raw["value"]
        else:
            break
    return raw


def _parse_scraped_row(r: dict) -> dict | None:
    if not isinstance(r, dict):
        return None
    record = dict.fromkeys(OUTPUT_FIELDS, "")
    record.update({k: v for k, v in r.items() if k in OUTPUT_FIELDS})
    for field in ("Year", "Make", "Model"):
        if not record[field]:
            record[field] = r.get(field, "")
    # Normalize IAAI's local-time dates to the canonical UTC form so every
    # consumer (price CSVs, workbook, HTML) sees the same shape.
    if record.get(AUCTION_DATE_COL):
        record[AUCTION_DATE_COL] = normalize_auction_date(record[AUCTION_DATE_COL])
    record["_full_title"] = r.get("_full_title", "")
    return record if record.get("Link") else None


def apply_equipment_postfilter(page_records: list, equipment: str) -> list:
    if not equipment:
        return page_records
    kept = []
    for rec in page_records:
        if equipment_matches(rec.get("_full_title", ""), equipment):
            kept.append(rec)
    return kept


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class IAAIClient(Protocol):
    """Everything scripts need from iaai.com."""

    def __enter__(self) -> "IAAIClient": ...
    def __exit__(self, exc_type, exc, tb) -> None: ...

    def scrape_with_filters(self, filters: dict, clear_filters: bool = False) -> list[dict]:
        """Apply `filters`, walk every result page, return OUTPUT_FIELDS dicts."""


# ---------------------------------------------------------------------------
# Real (browser-backed) implementation
# ---------------------------------------------------------------------------

class BrowserIAAIClient:
    """Live iaai.com client backed by a real Chrome session (via nodriver)."""

    def __init__(
        self,
        browser_port: int | None = None,
        profile_dir: str | None = None,
    ) -> None:
        if not _NODRIVER_OK:
            raise RuntimeError("nodriver is required. Install with:  pip install nodriver")
        self._browser_port = browser_port
        self._profile_dir  = profile_dir
        self._browser      = None
        self._chrome_proc  = None
        self._page         = None
        self._has_run      = False

    # ---- Context manager ---------------------------------------------------

    def __enter__(self) -> "BrowserIAAIClient":
        asyncio.run(self._start())
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        asyncio.run(self._stop())

    async def _start(self) -> None:
        if self._browser_port:
            self._browser = await asyncio.wait_for(
                uc.start(host="127.0.0.1", port=self._browser_port),
                timeout=30.0,
            )
        else:
            port = _free_port()
            self._chrome_proc = await _start_chrome(port, self._profile_dir or "caches/chrome_profile_iaai")
            self._browser = await asyncio.wait_for(
                uc.start(host="127.0.0.1", port=port),
                timeout=30.0,
            )
        self._page = await asyncio.wait_for(
            self._browser.get(IAAI_SEARCH_URL), timeout=30.0,
        )
        await asyncio.sleep(WAIT_LONG)

    async def _stop(self) -> None:
        if self._chrome_proc:
            try:
                await asyncio.wait_for(self._browser.stop(), timeout=5.0)
            except Exception:
                pass
            self._chrome_proc.terminate()

    # ---- Public method ------------------------------------------------------

    def scrape_with_filters(self, filters: dict, clear_filters: bool = False) -> list[dict]:
        # IAAI requires clearing filters on the 2nd+ call in one session
        if not clear_filters and self._has_run:
            clear_filters = True
        self._has_run = True
        return asyncio.run(self._scrape_async(filters, clear_filters))

    async def _scrape_async(self, filters: dict, clear_filters: bool) -> list[dict]:
        page = self._page
        await page.get(IAAI_SEARCH_URL)
        await asyncio.sleep(WAIT_LONG)

        if clear_filters:
            await _clear_all_filters(page)

        make      = str(filters.get("make", "")).strip()
        models    = [str(m).strip() for m in (filters.get("models") or []) if str(m).strip()]
        year_min  = filters.get("year_min")
        year_max  = filters.get("year_max")
        odo_max   = filters.get("odometer_max")
        fuel_type = str(filters.get("fuel_type", "")).strip()
        equipment = str(filters.get("equipment", "")).strip()

        await _apply_featured_filter(page, "Run & Drive")
        await _apply_featured_filter(page, "Auction Today")
        await _apply_year_filter(page, year_min, year_max)
        await _apply_make_filter(page, make)
        if not await _apply_model_filters(page, models):
            return []
        await _apply_fuel_type_filter(page, fuel_type)
        await _apply_odometer_filter(page, odo_max)
        await asyncio.sleep(WAIT_MEDIUM)

        all_records: list[dict] = []
        page_num    = 1
        total_pages = await _get_total_pages(page)
        while True:
            page_records = await _scrape_current_page(page)
            all_records.extend(apply_equipment_postfilter(page_records, equipment))
            if page_num >= total_pages:
                break
            if not await _go_to_next_page(page):
                break
            page_num += 1
        return all_records


# ---------------------------------------------------------------------------
# Fake (in-memory) implementation for tests
# ---------------------------------------------------------------------------

class FakeIAAIClient:
    """In-memory IAAIClient.

    Two modes:
      - flat:      FakeIAAIClient(rows=[...])                returns same list every call
      - callable:  FakeIAAIClient(scrape_fn=lambda f: ...)   compute per-filter result
    """

    def __init__(
        self,
        rows: list[dict] | None = None,
        scrape_fn: Callable[[dict], list[dict]] | None = None,
    ) -> None:
        self._rows      = list(rows or [])
        self._scrape_fn = scrape_fn
        self.calls: list[dict] = []

    def __enter__(self) -> "FakeIAAIClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Fake has no browser to tear down; context manager is for API parity.
        return None

    def scrape_with_filters(self, filters: dict, clear_filters: bool = False) -> list[dict]:
        del clear_filters  # accepted for protocol compatibility; fake has no UI state
        self.calls.append(dict(filters))
        if self._scrape_fn is not None:
            return list(self._scrape_fn(filters))
        return list(self._rows)


# ---------------------------------------------------------------------------
# Browser startup (private)
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cdp_ready(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


async def _start_chrome(port: int, profile_dir: str) -> asyncio.subprocess.Process:
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    chrome_exe = find_chrome()
    proc = await asyncio.create_subprocess_exec(
        chrome_exe,
        f"--remote-debugging-port={port}",
        "--remote-debugging-host=127.0.0.1",
        "--no-first-run",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-session-crashed-bubble",
        "--window-size=1400,900",
        f"--user-data-dir={profile_dir}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(15.0):
            while not _cdp_ready("127.0.0.1", port):
                await asyncio.sleep(0.3)
    except TimeoutError:
        proc.terminate()
        raise RuntimeError(f"Chrome did not expose CDP on port {port} within 15s")
    return proc


# ---------------------------------------------------------------------------
# Page interaction helpers (private)
# ---------------------------------------------------------------------------

async def _clear_all_filters(page) -> bool:
    js = """
    (function() {
        var link = document.querySelector('a.link[data-bind*="ClearFilters"]');
        if (link) { link.click(); return true; }
        var links = document.querySelectorAll('a.link');
        for (var l of links) {
            if (l.innerText.trim().toLowerCase() === 'clear all filters') {
                l.click();
                return true;
            }
        }
        return false;
    })();
    """
    ok = await page.evaluate(js)
    if ok:
        await asyncio.sleep(WAIT_MEDIUM * 1.5)
    return bool(ok)


async def _set_input_value(page, element_id: str, value: str) -> bool:
    js = f"""
    (function() {{
        var el = document.getElementById('{element_id}');
        if (!el) return false;
        var setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value').set;
        setter.call(el, '{value}');
        el.dispatchEvent(new Event('input',  {{bubbles: true}}));
        el.dispatchEvent(new Event('change', {{bubbles: true}}));
        el.dispatchEvent(new KeyboardEvent('keyup', {{bubbles: true}}));
        return true;
    }})();
    """
    return await page.evaluate(js)


async def _click_checkbox_by_name(page, name_value: str) -> bool:
    js = f"""
    (function() {{
        var inputs = document.querySelectorAll('input[type="checkbox"]');
        var target = '{name_value.upper()}';
        for (var inp of inputs) {{
            if ((inp.name || '').toUpperCase() === target) {{
                if (!inp.checked) inp.click();
                return true;
            }}
        }}
        for (var inp of inputs) {{
            if ((inp.name || '').toUpperCase().includes(target)) {{
                if (!inp.checked) inp.click();
                return true;
            }}
        }}
        return false;
    }})();
    """
    return await page.evaluate(js)


async def _type_in_filter_search(page, placeholder_keyword: str, search_value: str) -> bool:
    js = f"""
    (function() {{
        var inputs = document.querySelectorAll('input.keysearch-filter');
        for (var inp of inputs) {{
            if ((inp.placeholder || '').toLowerCase().includes('{placeholder_keyword.lower()}')) {{
                var setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                setter.call(inp, '{search_value}');
                inp.dispatchEvent(new Event('input',  {{bubbles: true}}));
                inp.dispatchEvent(new KeyboardEvent('keyup', {{bubbles: true}}));
                return true;
            }}
        }}
        return false;
    }})();
    """
    return await page.evaluate(js)


async def _apply_featured_filter(page, filter_id: str) -> bool:
    js = f"""
    (function() {{
        var btn = document.getElementById('{filter_id}');
        if (btn) {{ btn.click(); return true; }}
        return false;
    }})();
    """
    ok = await page.evaluate(js)
    await asyncio.sleep(WAIT_SHORT)
    return bool(ok)


async def _apply_year_filter(page, year_min, year_max) -> None:
    year_min = str(year_min) if year_min else ""
    year_max = str(year_max) if year_max else ""
    if not year_min and not year_max:
        return
    if year_min:
        await _set_input_value(page, "YearFilterFrom", year_min)
        await asyncio.sleep(0.3)
    if year_max:
        await _set_input_value(page, "YearFilterTo", year_max)
        await asyncio.sleep(0.3)
    js = """
    (function() {
        var btns = document.querySelectorAll('#YearFilter button');
        for (var b of btns) {
            if (b.innerText.includes('Apply')) { b.click(); return true; }
        }
        return false;
    })();
    """
    await page.evaluate(js)
    await asyncio.sleep(WAIT_MEDIUM)


async def _apply_odometer_filter(page, odo_max) -> None:
    odo_max = str(odo_max) if odo_max else ""
    if not odo_max:
        return
    await _set_input_value(page, "ODOValueFilterTo", odo_max)
    await asyncio.sleep(0.3)
    js = """
    (function() {
        var btns = document.querySelectorAll('#ODOValueFilter button');
        for (var b of btns) {
            if (b.innerText.includes('Apply')) { b.click(); return true; }
        }
        return false;
    })();
    """
    await page.evaluate(js)
    await asyncio.sleep(WAIT_MEDIUM)


async def _apply_make_filter(page, make: str) -> None:
    if not make:
        return
    await _type_in_filter_search(page, "make", make)
    await asyncio.sleep(WAIT_SHORT)
    await _click_checkbox_by_name(page, make)
    await asyncio.sleep(WAIT_MEDIUM)


async def _apply_model_filter(page, model: str) -> bool:
    if not model:
        return True
    await _type_in_filter_search(page, "model", model)
    await asyncio.sleep(WAIT_SHORT)
    ok = await _click_checkbox_by_name(page, model)
    await asyncio.sleep(WAIT_MEDIUM)
    return bool(ok)


async def _apply_fuel_type_filter(page, fuel_type: str) -> None:
    if not fuel_type:
        return
    await _click_checkbox_by_name(page, fuel_type)
    await asyncio.sleep(WAIT_MEDIUM)


async def _apply_model_filters(page, models: list) -> bool:
    applied = []
    for m in models:
        ok = await _apply_model_filter(page, m)
        if ok:
            applied.append(m)
    if models and not applied:
        return False
    return True


async def _scrape_current_page(page) -> list[dict]:
    js = """
    (function() {
        var results = [];
        var rows = document.querySelectorAll('.table-row.table-row-border');
        rows.forEach(function(row) {
            var rec = {};
            var heading = row.querySelector('.table-cell--heading a');
            if (heading) {
                rec['_full_title'] = heading.innerText.trim();
                rec['Link']        = heading.href;
                var parts  = rec['_full_title'].split(' ');
                rec['Year']  = parts[0] || '';
                rec['Make']  = parts[1] || '';
                rec['Model'] = parts.slice(2).join(' ');
            }
            var stockEl = row.querySelector('[title^="Stock #"]');
            if (stockEl) rec['Lot Number'] = stockEl.innerText.trim();
            var dmgEl = row.querySelector('[title^="Primary Damage"]');
            if (dmgEl) rec['Primary Damage'] = dmgEl.innerText.trim();
            var odoEl = row.querySelector('[title^="Odometer"]');
            if (odoEl) rec['Odometer'] = odoEl.innerText.trim().replace(/[^\\d,]/g, '');
            var fuelEl = row.querySelector('[title^="Fuel Type"]');
            if (fuelEl) rec['Fuel Type'] = fuelEl.innerText.trim();
            var locEl = row.querySelector('.data-list--data a[aria-label="Branch Name"]');
            if (locEl) rec['Location'] = locEl.innerText.trim();
            var dateEl = row.querySelector('.data-list__value--action');
            if (dateEl) rec['Auction Date'] = dateEl.innerText.trim();
            var priceEl = row.querySelector('[title^="ACV:"]');
            if (priceEl) {
                var ptext = priceEl.getAttribute('title') || '';
                rec['ACV'] = ptext.replace('ACV: ', '').trim();
            }
            results.push(rec);
        });
        return JSON.stringify(results);
    })();
    """
    raw_str = _unwrap_evaluate_result(await page.evaluate(js))
    if not raw_str:
        return []
    try:
        rows = json.loads(raw_str)
    except (json.JSONDecodeError, TypeError):
        return []
    return [r for r in (_parse_scraped_row(row) for row in rows) if r is not None]


async def _get_total_pages(page) -> int:
    js = """
    (function() {
        var el = document.querySelector('.pages-count span:last-child');
        return el ? parseInt(el.innerText) : 1;
    })();
    """
    try:
        val = await page.evaluate(js)
        return int(val) if val else 1
    except Exception:
        return 1


async def _go_to_next_page(page) -> bool:
    js = """
    (function() {
        var btn = document.querySelector('.btn-next');
        if (btn && !btn.disabled) { btn.click(); return true; }
        return false;
    })();
    """
    ok = await page.evaluate(js)
    if ok:
        await asyncio.sleep(WAIT_MEDIUM)
    return bool(ok)
