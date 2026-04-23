"""
bid.cars Price Scraper
======================
Reads a CSV with a "Lot Number" column, fetches the final bid price
from https://bid.cars/en/lot/1-{lot}/ for each row, and writes the
result to a new CSV with an extra "Last Price" column.

The site renders prices via JavaScript, so we use a real Chrome browser
(nodriver) — no CAPTCHA required.

INSTALL:
  pip install nodriver beautifulsoup4 lxml

  Chrome or Chromium must be installed on your system.

USAGE:
  python bidfax_scraper.py --input input.csv --output output.csv
  python bidfax_scraper.py --input input.csv --output output.csv --delay 3
  python bidfax_scraper.py --input input.csv --output output.csv --dump-html
"""

import argparse
import asyncio
import csv
import re
import sys
from pathlib import Path

try:
    import nodriver as uc
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit(
        "Missing dependencies. Run:\n"
        "  pip install nodriver beautifulsoup4 lxml\n"
        "Chrome/Chromium must also be installed."
    )

LOT_URL_TEMPLATE = "https://bid.cars/en/lot/{prefix}-{lot}/"
AUCTION_PREFIX   = {"copart": "1", "iaai": "0"}
DUMP_HTML = False


# ── Price extraction ──────────────────────────────────────────────────────────

def extract_price(html: str, lot: str = "") -> str:
    if DUMP_HTML and lot:
        path = f"debug_{lot}.html"
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[saved {path}]", end=" ")

    soup = BeautifulSoup(html, "lxml")

    # Primary: <div class="lot-price-info"> > <span class="price current_bid">
    container = soup.find("div", class_="lot-price-info")
    if container:
        state = container.find("div", class_="field-name")
        if state:
            raw = state.get_text(strip=True).lower()
            if raw != "final bid":
                return "In Progress"
        span = container.find("span", class_="current_bid")
        if span:
            raw = re.sub(r"[^\d]", "", span.get_text())
            if raw.isdigit() and int(raw) >= 100:
                return f"${int(raw):,}"

    # Fallback: any span with class current_bid anywhere on page
    span = soup.find("span", class_="current_bid")
    if span:
        raw = re.sub(r"[^\d]", "", span.get_text())
        if raw.isdigit() and int(raw) >= 100:
            return f"${int(raw):,}"
    
    return "None"

# ── VIN extraction ──────────────────────────────────────────────────────────

def extract_VIN(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")

    vin = soup.find("span", class_="vin-drop")
    if vin:
        return vin.get_text(strip=True).upper()
   
    return "None"


# ── Browser session ───────────────────────────────────────────────────────────

async def run_all(rows: list[dict], delay: float, render_wait: int, lot_prefix: str) -> list[str]:
    browser = await uc.start(
        headless=False,
        sandbox=False,
        browser_args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    prices = []
    
    try:
        for i, row in enumerate(rows):
            if (row.get("Make","").strip().startswith("#")):
                continue
            lot = str(row.get("Lot Number", "")).strip()
            if not lot:
                print(f"  [{i+1}/{len(rows)}] SKIP — empty Lot Number")
                prices.append("None")
                continue

            print(f"  [{i+1}/{len(rows)}] Lot {lot} … ", end="", flush=True)
            url = LOT_URL_TEMPLATE.format(prefix=lot_prefix, lot=lot)

            try:
                page = await browser.get(url)
                await asyncio.sleep(render_wait)
                html = await page.get_content()
                price = extract_price(html, lot)
                vin = extract_VIN(html)
            except Exception as exc:
                print(f"[ERR: {exc}]", end=" ")
                price = "None"
                vin = "None"

            print(price)
            prices.append(f"{price} | VIN: {vin}")
            
            if i + 1 < len(rows):
                await asyncio.sleep(delay)

    finally:
        try:
            browser.stop()
        except Exception:
            pass

    return prices


# ── Main ──────────────────────────────────────────────────────────────────────

def process(input_path: Path, output_path: Path, delay: float, render_wait: int, lot_prefix: str):
    with input_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            sys.exit("Input CSV is empty.")
        if "Lot Number" not in reader.fieldnames:
            sys.exit(f"'Lot Number' column not found. Available: {list(reader.fieldnames)}")
        rows = list(reader)
        fieldnames = list(reader.fieldnames)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        prices = loop.run_until_complete(run_all(rows, delay, render_wait, lot_prefix))
    finally:
        loop.close()

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames + ["Last Price"])
        writer.writeheader()
        for row, price in zip(rows, prices):
            row["Last Price"] = price
            writer.writerow(row)

    print(f"\nDone. Saved -> {output_path}")


def main():
    from datetime import date, timedelta
    global DUMP_HTML
    today = (date.today() - timedelta(days=1)).strftime("%Y_%m_%d")

    parser = argparse.ArgumentParser(description="Scrape bid.cars prices by Lot Number.")
    parser.add_argument("--auction",     default="copart",
                        help="Auction name: copart or iaai (default: copart)")
    parser.add_argument("--output",      default=None,
                        help="Output CSV (default: <auction>_price_YYYY_MM_DD.csv)")
    parser.add_argument("--delay",       type=float, default=2.0,
                        help="Seconds between lots (default: 2.0)")
    parser.add_argument("--render-wait", type=int,   default=5,
                        help="Seconds to wait for JS to render price (default: 5)")
    parser.add_argument("--dump-html",   action="store_true",
                        help="Save raw HTML for each lot to debug_<lot>.html")
    args = parser.parse_args()

    DUMP_HTML = args.dump_html

    input_path  = Path(f"{args.auction}_search_{today}.csv")
    output_path = Path(args.output or f"{args.auction}_price_{today}.csv")

    if not input_path.exists():
        sys.exit(f"Input file not found: {input_path}")

    print(f"Auction     : {args.auction}")
    print(f"Input       : {input_path}")
    print(f"Output      : {output_path}")
    print(f"Delay       : {args.delay}s between lots")
    print(f"Render wait : {args.render_wait}s for JS")
    print(f"Dump HTML   : {DUMP_HTML}\n")

    lot_prefix = AUCTION_PREFIX.get(args.auction, "1")
    process(input_path, output_path, args.delay, args.render_wait, lot_prefix)


if __name__ == "__main__":
    main()