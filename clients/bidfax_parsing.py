"""Pure HTML / URL parsing for bidfax.info results.

Split out from `clients.bidfax` so this module has zero browser/asyncio
dependencies — easy to unit-test, and importable by tooling that just
needs to recognise bidfax URLs.
"""

from __future__ import annotations

import re

try:
    from bs4 import BeautifulSoup
    _DEPS_OK = True
except ImportError:
    _DEPS_OK = False


IN_PROGRESS = "In Progress"

# Bidfax search-result URL pattern, e.g.
#   https://bidfax.info/honda/cr-v/36735445-honda-cr-v-2024-blue-vin-XYZ.html
RESULT_URL_RE = re.compile(r'^https://bidfax\.info/[^/]+/[^/]+/.+\.html$')

# Trailing `-vin-XYZ.html` segment used to extract the VIN from the URL.
VIN_FROM_URL_RE = re.compile(r'-vin-([a-z0-9]+)\.html$', re.IGNORECASE)


def url_make_matches(csv_make: str, bidfax_url: str) -> bool:
    """Loose make-equality check between the CSV-side make string and the
    first path segment of a bidfax result URL. Bidfax slugifies makes
    differently from raw display strings, so allow startswith() in either
    direction."""
    parts    = bidfax_url.replace("https://bidfax.info/", "").split("/")
    url_make = parts[0].lower() if parts else ""
    norm     = re.sub(r"[\s_]+", "-", csv_make.strip().lower())
    return bool(url_make) and (url_make == norm
                               or norm.startswith(url_make)
                               or url_make.startswith(norm))


def extract_grid_result(html: str) -> tuple[str, str, str] | None:
    """Parse bidfax results-page HTML. Returns (price, vin, url) or None.

    None means "the page didn't contain a result grid" — most often the
    homepage shown after a soft-block, or a genuine no-result. Callers
    distinguish via separate `homepage marker` / bounce detection.
    """
    if not _DEPS_OK:
        return None
    soup = BeautifulSoup(html, "lxml")
    grid = soup.find(id="grid")
    if not grid:
        return None
    # bs4's a["href"] is `str | AttributeValueList` in modern type stubs —
    # cast to str so the regex calls type-check and the return tuple stays
    # `tuple[str, str, str]`.
    url = next(
        (str(a["href"]) for a in grid.find_all("a", href=True)
         if RESULT_URL_RE.match(str(a["href"]))),
        None,
    )
    if not url:
        return None
    m_vin = VIN_FROM_URL_RE.search(url)
    vin   = m_vin.group(1).upper() if m_vin else ""
    price = IN_PROGRESS
    span  = grid.find("span", class_="prices")
    if span:
        raw = span.get_text(strip=True)
        if raw.isdigit():
            price = f"${int(raw):,}"
    return price, vin, url
