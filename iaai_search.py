#!/usr/bin/env python3
"""
IAAI.com scraper using nodriver.
Reads filters from a CSV file and outputs results to a CSV file.

Usage:
    python iaai_scraper.py [--input input.csv] [--output output.csv]

Input CSV format (key: value pairs, comma-separated, one search per line):
    Make: Lincoln, Model: Nautilus, Year min: 2024, Year max: 2027, Odometer max: 30000
    Make: Mercedes-Benz, Model: Gle, Year min: 2022, Year max: 2025, Odometer max: 25000, Fuel Type: Hybrid Engine, Equipment: 4MATIC
    Make: Audi, Model: Q5, Year min: 2021, Year max: 2024, Odometer max: 40000, Equipment: Premium 45

Equipment post-filter:
    All words in the Equipment value must appear in the vehicle's full title,
    in any order (case-insensitive). E.g. "Premium 45" matches:
        Q5 PREMIUM PLUS 45 TFSI S LINE QUATTRO  (pass)
        Q5 45 PREMIUM PLUS TFSI S LINE QUATTRO  (pass)
        Q5 PREMIUM PLUS TFSI S LINE QUATTRO     (fail - missing "45")
"""

import asyncio
import argparse
import csv
import json
import re
import socket
import sys
from pathlib import Path

try:
    import nodriver as uc
except ImportError:
    print("nodriver not found. Install it with: pip install nodriver")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IAAI_SEARCH_URL = "https://www.iaai.com/Search"
OUTPUT_FIELDS = [
    "Make", "Model", "Year", "Odometer", "Fuel Type",
    "Lot Number", "Link", "Auction Date", "Location",
    "Primary Damage", "ACV",
]
WAIT_SHORT  = 1.5   # seconds - brief pause after click
WAIT_MEDIUM = 3.0   # seconds - wait for filter results to reload
WAIT_LONG   = 5.0   # seconds - initial page load


# ---------------------------------------------------------------------------
# Equipment post-filter
# ---------------------------------------------------------------------------

def equipment_matches(full_title: str, equipment: str) -> bool:
    """Return True when every word in `equipment` appears in `full_title`
    (case-insensitive, any order, whole-word matching).

    Examples:
        equipment  = "Premium 45"
        full_title = "2022 AUDI Q5 PREMIUM PLUS 45 TFSI S LINE QUATTRO"  -> True
        full_title = "2022 AUDI Q5 PREMIUM PLUS TFSI S LINE QUATTRO"     -> False
        full_title = "2022 AUDI Q5 45 PREMIUM PLUS TFSI S LINE QUATTRO"  -> True
    """
    if not equipment:
        return True  # no equipment filter -> always passes

    # Split the title into individual tokens (whitespace-separated)
    title_tokens = set(re.findall(r'\S+', full_title.upper()))
    # Every required word must appear as a token in the title
    required_words = [w.upper() for w in equipment.split() if w.strip()]

    return all(word in title_tokens for word in required_words)


# ---------------------------------------------------------------------------
# Input CSV parsing
# ---------------------------------------------------------------------------

def _reassemble_segments(raw_line: str) -> list[str]:
    """Split a comma-separated line into key:value segments.

    Commas that appear inside a value (no colon in that part) are re-joined
    with their preceding segment so values like "Gle Class" survive intact.
    """
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
    """Write one normalised key/value pair into the filters dict."""
    if key == "make":
        filters["make"] = val.upper()
    elif key == "model":
        models = [v.strip().upper() for v in val.split(";") if v.strip()]
        filters["models"] = models if models else [val.upper()]
    elif key in ("year_min", "yearmin"):
        try:
            filters["year_min"] = int(val)
        except ValueError:
            pass
    elif key in ("year_max", "yearmax"):
        try:
            filters["year_max"] = int(val)
        except ValueError:
            pass
    elif key in ("odometer_max", "odo_max", "odometer"):
        try:
            filters["odometer_max"] = int(val)
        except ValueError:
            pass
    elif key in ("fuel_type", "fueltype", "fuel"):
        filters["fuel_type"] = val.strip()
    elif key == "equipment":
        filters["equipment"] = val.strip()


def parse_filter_row(raw_line: str) -> dict:
    """Parse one line of 'Key: Value' comma-separated segments.

    Recognised keys (case-insensitive):
        Make, Model, Year min, Year max, Odometer max, Fuel Type, Equipment
    """
    filters: dict = {}
    for seg in _reassemble_segments(raw_line):
        m = re.match(r"^([^:]+?)\s*:\s*(.+)$", seg.strip())
        if not m:
            continue
        key = m.group(1).strip().lower().replace(" ", "_")
        _apply_segment(filters, key, m.group(2).strip())
    return filters


def read_filters_csv(path: str) -> list[dict]:
    """Read the input CSV. Multi-model rows stay as one entry with a 'models' list.

    Lines starting with '#' or blank lines are ignored.
    """
    filter_list: list[dict] = []

    with open(path, newline="", encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            parsed = parse_filter_row(line)
            if not parsed:
                continue

            filter_list.append(parsed)

    return filter_list


# ---------------------------------------------------------------------------
# Output CSV
# ---------------------------------------------------------------------------

def write_output_csv(path: str, records: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    print(f"[+] Saved {len(records)} record(s) -> {path}")


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

async def clear_all_filters(page) -> bool:
    """Click 'Clear All Filters' if visible, then wait for the page to reset."""
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
        print("    [+] Cleared all previous filters")
        await asyncio.sleep(WAIT_MEDIUM * 1.5)
    else:
        print("    [info] No active filters to clear")
    return bool(ok)


async def set_input_value(page, element_id: str, value: str) -> bool:
    """Set an <input> value and fire React/KO-compatible change events."""
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


async def click_checkbox_by_name(page, name_value: str) -> bool:
    """Tick the first matching (unchecked) checkbox: exact name first, then partial."""
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


async def type_in_filter_search(page, placeholder_keyword: str, search_value: str) -> bool:
    """Type into the small search box above a filter's checkbox list."""
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


# ---------------------------------------------------------------------------
# Individual filter appliers
# ---------------------------------------------------------------------------

async def apply_featured_filter(page, filter_id: str) -> bool:
    """Click a featured-filter button by its HTML element id."""
    js = f"""
    (function() {{
        var btn = document.getElementById('{filter_id}');
        if (btn) {{ btn.click(); return true; }}
        return false;
    }})();
    """
    ok = await page.evaluate(js)
    if ok:
        print(f"    [+] Clicked featured filter: {filter_id}")
    else:
        print(f"    [warn] Featured filter button not found: {filter_id}")
    await asyncio.sleep(WAIT_SHORT)
    return bool(ok)


async def apply_year_filter(page, year_min, year_max) -> None:
    year_min = str(year_min) if year_min else ""
    year_max = str(year_max) if year_max else ""
    if not year_min and not year_max:
        return
    print(f"    [+] Setting Year: {year_min} - {year_max}")
    if year_min:
        await set_input_value(page, "YearFilterFrom", year_min)
        await asyncio.sleep(0.3)
    if year_max:
        await set_input_value(page, "YearFilterTo", year_max)
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


async def apply_odometer_filter(page, odo_max) -> None:
    odo_max = str(odo_max) if odo_max else ""
    if not odo_max:
        return
    print(f"    [+] Setting Odometer max: {odo_max}")
    await set_input_value(page, "ODOValueFilterTo", odo_max)
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


async def apply_make_filter(page, make: str) -> None:
    if not make:
        return
    print(f"    [+] Setting Make: {make}")
    await type_in_filter_search(page, "make", make)
    await asyncio.sleep(WAIT_SHORT)
    ok = await click_checkbox_by_name(page, make)
    if not ok:
        print(f"    [warn] Make checkbox not found: {make}")
    await asyncio.sleep(WAIT_MEDIUM)


async def apply_model_filter(page, model: str) -> bool:
    if not model:
        return True
    print(f"    [+] Setting Model: {model}")
    await type_in_filter_search(page, "model", model)
    await asyncio.sleep(WAIT_SHORT)
    ok = await click_checkbox_by_name(page, model)
    if not ok:
        print(f"    [warn] Model checkbox not found: {model}")
    await asyncio.sleep(WAIT_MEDIUM)
    return bool(ok)


async def apply_fuel_type_filter(page, fuel_type: str) -> None:
    if not fuel_type:
        return
    print(f"    [+] Setting Fuel Type: {fuel_type}")
    ok = await click_checkbox_by_name(page, fuel_type)
    if not ok:
        print(f"    [warn] Fuel Type checkbox not found: {fuel_type}")
    await asyncio.sleep(WAIT_MEDIUM)


# ---------------------------------------------------------------------------
# Result scraping
# ---------------------------------------------------------------------------

def _unwrap_evaluate_result(raw):
    """Unwrap a nodriver evaluate() result that may be nested inside {'value': ...}."""
    for _ in range(5):
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict) and "value" in raw:
            raw = raw["value"]
        else:
            break
    return raw


def _parse_scraped_row(r: dict) -> dict | None:
    """Convert a raw JS row dict to an OUTPUT_FIELDS record, or None if invalid."""
    if not isinstance(r, dict):
        print(f"    [warn] Skipping non-dict row: {type(r)} {str(r)[:100]}")
        return None
    record = dict.fromkeys(OUTPUT_FIELDS, "")
    record.update({k: v for k, v in r.items() if k in OUTPUT_FIELDS})
    for field in ("Year", "Make", "Model"):
        if not record[field]:
            record[field] = r.get(field, "")
    record["_full_title"] = r.get("_full_title", "")
    return record if record.get("Link") else None


async def scrape_current_page(page) -> list[dict]:
    """Extract all vehicle rows visible on the current results page.

    The JS returns the data as a JSON *string* (via JSON.stringify) so that
    nodriver/CDP never gets a chance to mangle the object into an unrecognised
    structure.  We parse it back on the Python side with the standard `json`
    module, guaranteeing clean dicts every time.

    Each returned dict contains OUTPUT_FIELDS plus a private '_full_title'
    key used by the equipment post-filter (dropped before writing to CSV by
    DictWriter's extrasaction='ignore').
    """
    js = """
    (function() {
        var results = [];
        var rows = document.querySelectorAll('.table-row.table-row-border');
        rows.forEach(function(row) {
            var rec = {};

            // Full title + link
            var heading = row.querySelector('.table-cell--heading a');
            if (heading) {
                rec['_full_title'] = heading.innerText.trim();
                rec['Link']        = heading.href;
                var parts  = rec['_full_title'].split(' ');
                rec['Year']  = parts[0] || '';
                rec['Make']  = parts[1] || '';
                // Everything after year and make is the model + trim line
                rec['Model'] = parts.slice(2).join(' ');
            }

            // Lot / stock number
            var stockEl = row.querySelector('[title^="Stock #"]');
            if (stockEl) rec['Lot Number'] = stockEl.innerText.trim();

            // Primary damage
            var dmgEl = row.querySelector('[title^="Primary Damage"]');
            if (dmgEl) rec['Primary Damage'] = dmgEl.innerText.trim();

            // Odometer (digits and commas only)
            var odoEl = row.querySelector('[title^="Odometer"]');
            if (odoEl) rec['Odometer'] = odoEl.innerText.trim().replace(/[^\\d,]/g, '');

            // Fuel type
            var fuelEl = row.querySelector('[title^="Fuel Type"]');
            if (fuelEl) rec['Fuel Type'] = fuelEl.innerText.trim();

            // Location / branch
            var locEl = row.querySelector('.data-list--data a[aria-label="Branch Name"]');
            if (locEl) rec['Location'] = locEl.innerText.trim();

            // Auction date/time
            var dateEl = row.querySelector('.data-list__value--action');
            if (dateEl) rec['Auction Date'] = dateEl.innerText.trim();

            // ACV
            var priceEl = row.querySelector('[title^="ACV:"]');
            if (priceEl) {
                var ptext = priceEl.getAttribute('title') || '';
                rec['ACV'] = ptext.replace('ACV: ', '').trim();
            }

            results.push(rec);
        });
        // Return as a JSON string so CDP never re-serialises the object
        return JSON.stringify(results);
    })();
    """
    raw_str = _unwrap_evaluate_result(await page.evaluate(js))

    if not raw_str:
        return []

    try:
        rows = json.loads(raw_str)
    except (json.JSONDecodeError, TypeError) as exc:
        print(f"    [warn] JSON parse error in scrape_current_page: {exc}")
        print(f"    [warn] raw value type={type(raw_str)}, preview={str(raw_str)[:200]}")
        return []

    return [r for r in (_parse_scraped_row(row) for row in rows) if r is not None]


async def get_total_pages(page) -> int:
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


async def go_to_next_page(page) -> bool:
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


# ---------------------------------------------------------------------------
# Per-filter-row workflow
# ---------------------------------------------------------------------------

async def apply_model_filters(page, models: list) -> bool:
    """Apply all requested model checkboxes. Returns False if none could be applied."""
    applied = []
    for m in models:
        ok = await apply_model_filter(page, m)
        if ok:
            applied.append(m)
        else:
            print(f"    [!] Model '{m}' not found on site — skipping this model.")
    if models and not applied:
        print("    [!] No requested models available on site — skipping scrape.")
        return False
    return True


def apply_equipment_postfilter(page_records: list, equipment: str) -> list:
    """Drop records whose title does not contain all equipment keywords."""
    if not equipment:
        print(f"        -> {len(page_records)} vehicle(s) found")
        return page_records
    kept = []
    for rec in page_records:
        title = rec.get("_full_title", "")
        if equipment_matches(title, equipment):
            kept.append(rec)
        else:
            print(f"        [post-filter] drop  {title!r}")
    dropped = len(page_records) - len(kept)
    print(f"        -> {len(page_records)} found, {dropped} dropped by "
          f"equipment filter, {len(kept)} kept")
    return kept


async def scrape_with_filters(page, filters: dict, clear_filters: bool = False) -> list[dict]:
    """Navigate to IAAI, apply all filters, scrape every page, post-filter."""

    make      = str(filters.get("make",         "")).strip()
    raw_models = filters.get("models") or ([filters["model"]] if filters.get("model") else [])
    models    = [str(m).strip() for m in raw_models if str(m).strip()]
    year_min  = filters.get("year_min")
    year_max  = filters.get("year_max")
    odo_max   = filters.get("odometer_max")
    fuel_type = str(filters.get("fuel_type",    "")).strip()
    equipment = str(filters.get("equipment",    "")).strip()

    print(f"\n[*] Filters -> Make={make!r}, Models={models}, "
          f"Year={year_min}-{year_max}, Odo<={odo_max}, "
          f"Fuel={fuel_type!r}, Equipment={equipment!r}")

    await page.get(IAAI_SEARCH_URL)
    await asyncio.sleep(WAIT_LONG)

    if clear_filters:
        await clear_all_filters(page)

    await apply_featured_filter(page, "Run & Drive")
    await apply_featured_filter(page, "Auction Today")
    await apply_year_filter(page, year_min, year_max)
    await apply_make_filter(page, make)

    if not await apply_model_filters(page, models):
        return []

    await apply_fuel_type_filter(page, fuel_type)
    await apply_odometer_filter(page, odo_max)
    await asyncio.sleep(WAIT_MEDIUM)

    all_records: list[dict] = []
    page_num    = 1
    total_pages = await get_total_pages(page)
    print(f"    [*] Total pages detected: {total_pages}")

    while True:
        print(f"    [*] Scraping page {page_num}/{total_pages} ...")
        page_records = await scrape_current_page(page)
        all_records.extend(apply_equipment_postfilter(page_records, equipment))

        if page_num >= total_pages:
            break
        if not await go_to_next_page(page):
            break
        page_num += 1

    print(f"    [+] Total kept for this filter set: {len(all_records)}")
    return all_records


# ---------------------------------------------------------------------------
# Browser startup helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cdp_ready(host: str, port: int) -> bool:
    """Return True if Chrome's CDP HTTP endpoint is answering."""
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


async def _start_chrome(
    port: int, profile_dir: str
) -> asyncio.subprocess.Process:
    """Launch Chrome with a remote-debugging port and wait until CDP is ready."""
    exe = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    proc = await asyncio.create_subprocess_exec(
        exe,
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
# Entry point
# ---------------------------------------------------------------------------


async def main(input_path: str, output_path: str, profile_dir: str,
               browser_port: int | None = None) -> None:
    print(f"[*] Reading filters from: {input_path}")
    filter_rows = read_filters_csv(input_path)
    if not filter_rows:
        print("[!] No filter rows found in input CSV. Exiting.")
        return
    print(f"[*] {len(filter_rows)} filter set(s) to process.")

    if browser_port:
        print(f"[*] Connecting to shared Chrome on port {browser_port} …")
        chrome_proc = None
        browser = await asyncio.wait_for(
            uc.start(host="127.0.0.1", port=browser_port),
            timeout=30.0,
        )
    else:
        port = _free_port()
        print(f"[*] Starting Chrome on port {port} (profile: {profile_dir}) …")
        chrome_proc = await _start_chrome(port, profile_dir)
        print("[*] Chrome ready — connecting nodriver …")
        browser = await asyncio.wait_for(
            uc.start(host="127.0.0.1", port=port),
            timeout=30.0,
        )

    page = await asyncio.wait_for(browser.get(IAAI_SEARCH_URL), timeout=30.0)
    await asyncio.sleep(WAIT_LONG)

    all_results: list[dict] = []
    try:
        for idx, filters in enumerate(filter_rows, 1):
            print(f"\n{'='*60}")
            print(f"[*] Filter set {idx}/{len(filter_rows)}")
            try:
                records = await scrape_with_filters(page, filters, clear_filters=(idx > 1))
                all_results.extend(records)
            except Exception as exc:
                print(f"[!] Error on filter set {idx}: {exc}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(3)
    finally:
        if chrome_proc:
            try:
                await browser.stop()
            except Exception:
                pass
            chrome_proc.terminate()

    write_output_csv(output_path, all_results)
    print(f"\n[+] Done. Total vehicles saved: {len(all_results)}")


def cli() -> None:
    from datetime import date
    today = date.today().strftime("%Y_%m_%d")

    parser = argparse.ArgumentParser(
        description="Scrape IAAI.com with filters defined in a CSV file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--input",  "-i", default="iaai_filters.csv",
                        help="Input CSV path (default: iaai_filters.csv)")
    parser.add_argument("--output", "-o", default=f"iaai_search_{today}.csv",
                        help=f"Output CSV path (default: iaai_search_{today}.csv)")
    parser.add_argument("--profile-dir",  "-p", default="caches/chrome_profile_iaai",
                        help="Persistent Chrome profile directory (default: caches/chrome_profile_iaai)")
    parser.add_argument("--browser-port", type=int, default=None,
                        help="Connect to a running Chrome on this port instead of launching one")
    args = parser.parse_args()

    if not Path(args.input).exists():
        sample = (
            "# One search per line - use 'Key: Value' pairs separated by commas\n"
            "# Separate multiple models with semicolons:  Model: Corsair;Nautilus\n"
            "# Equipment: all words must appear in the title (any order)\n"
            "#\n"
            "Make: Lincoln, Model: Nautilus, Year min: 2024, Year max: 2027, Odometer max: 30000\n"
            "Make: Audi, Model: Q5, Year min: 2021, Year max: 2024, Odometer max: 40000, Equipment: Premium 45\n"
            "Make: Mercedes-Benz, Model: Gle, Year min: 2022, Year max: 2025, "
            "Odometer max: 25000, Fuel Type: Hybrid Engine, Equipment: 4MATIC\n"
        )
        Path(args.input).write_text(sample, encoding="utf-8")
        print(f"[!] '{args.input}' not found - created a sample file. "
              "Edit it and re-run.")
        sys.exit(0)

    profile_dir = str(Path(args.profile_dir).resolve())
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    asyncio.run(main(args.input, args.output, profile_dir, browser_port=args.browser_port))


if __name__ == "__main__":
    cli()