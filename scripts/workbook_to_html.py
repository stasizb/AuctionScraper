#!/usr/bin/env python3
"""
Generate a beautiful HTML report from the auction results workbook.

Creates an output folder (default: html_report/) containing:
  index.html — one tab per Make, sortable + filterable tables
  style.css  — stylesheet
  script.js  — tab switching, column sorting, row filtering

The output folder is wiped and recreated on every run.

Usage:
    python workbook_to_html.py
    python workbook_to_html.py --workbook auction_results.xlsx --out html_report
    python workbook_to_html.py --title "My Auctions"
    python workbook_to_html.py --search-dir output --today-date 2026_04_10
"""

import argparse
import csv
import html as _html
import re
import shutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import openpyxl
except ImportError:
    sys.exit("openpyxl not found.  Install with:  pip install openpyxl")

from clients import bidfax
from core.dates import normalize_auction_date as _normalize_auction_date

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HYPERLINK_RE    = re.compile(r'=HYPERLINK\("([^"]+)"', re.IGNORECASE)
_PRICE_RE        = re.compile(r'^\$([\d,]+)$')
_NUMERIC_COLS    = {"Year", "Odometer", "Price"}
_AUCTION_DATE_COL = "Auction Date"

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

CSS = """\
:root {
  --bg:       #f0f2f5;
  --surface:  #ffffff;
  --primary:  #1a3a5c;
  --primary-h:#24507a;
  --accent:   #2563eb;
  --accent-h: #1d4ed8;
  --text:     #1e293b;
  --muted:    #64748b;
  --border:   #e2e8f0;
  --stripe:   #f8fafc;
  --hover:    #eff6ff;
  --green:    #16a34a;
  --today-bg: #f0fdf4;
  --today-hd: #166534;
  --radius:   8px;
  --shadow:   0 2px 16px rgba(0,0,0,.09);
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  font-size: 14px;
  line-height: 1.5;
}

/* ── Header ────────────────────────────────────────────────────── */
header {
  background: var(--primary);
  color: #fff;
  padding: 18px 32px;
  display: flex;
  align-items: baseline;
  gap: 14px;
}
header h1 { font-size: 1.4rem; font-weight: 700; letter-spacing: .3px; }
header .subtitle { font-size: .85rem; opacity: .6; }

/* ── Layout ────────────────────────────────────────────────────── */
.container { max-width: 1700px; margin: 0 auto; padding: 24px 24px 60px; }

/* ── Tabs ──────────────────────────────────────────────────────── */
.tab-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 16px;
}
.tab-btn {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 5px 14px;
  font-size: .82rem;
  font-weight: 500;
  color: var(--muted);
  cursor: pointer;
  transition: background .15s, color .15s, border-color .15s;
  white-space: nowrap;
}
.tab-btn:hover {
  background: var(--hover);
  color: var(--accent);
  border-color: var(--accent);
}
.tab-btn.active {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}
.badge {
  display: inline-block;
  border-radius: 10px;
  padding: 1px 7px;
  font-size: .72rem;
  margin-left: 5px;
  background: rgba(255,255,255,.25);
}
.tab-btn:not(.active) .badge {
  background: var(--border);
  color: var(--muted);
}

/* ── Model filter (uses <details>) ─────────────────────────────── */
.model-filter {
  margin-bottom: 12px;
  padding: 10px 14px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}
.model-filter[open] .model-chips { margin-top: 8px; }
.model-filter-label {
  font-size: .76rem;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .05em;
  cursor: pointer;
  list-style: none;
}
.model-filter-label::-webkit-details-marker { display: none; }
.model-filter-label::before {
  content: "▸ ";
  display: inline-block;
  transition: transform .15s;
}
.model-filter[open] .model-filter-label::before { content: "▾ "; }
.model-chips {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.model-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px;
  border: 1px solid var(--border);
  border-radius: 12px;
  font-size: .78rem;
  cursor: pointer;
  user-select: none;
  transition: border-color .12s, background .12s;
}
.model-chip:hover { border-color: var(--accent); background: var(--hover); }
.model-chip input[type="checkbox"] { accent-color: var(--accent); cursor: pointer; }
.model-chip.all-chip { font-weight: 600; }

/* ── Summary section (uses <details>) ──────────────────────────── */
.summary-section {
  margin-bottom: 14px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 12px 16px;
}
.summary-section[open] .summary-table { margin-top: 8px; }
.summary-label {
  font-size: .76rem;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .05em;
  cursor: pointer;
  list-style: none;
}
.summary-label::-webkit-details-marker { display: none; }
.summary-label::before {
  content: "▸ ";
  display: inline-block;
}
.summary-section[open] .summary-label::before { content: "▾ "; }
.summary-table {
  width: auto;
  font-size: .82rem;
  border-collapse: collapse;
}
.summary-table thead th {
  background: var(--bg);
  color: var(--text);
  font-size: .76rem;
  font-weight: 600;
  padding: 5px 20px 5px 8px;
  text-align: left;
  white-space: nowrap;
  cursor: default;
  border-bottom: 1px solid var(--border);
}
.summary-table tbody td {
  padding: 4px 20px 4px 8px;
  border-bottom: 1px solid var(--border);
}
.summary-table tbody tr:last-child td { border-bottom: none; }

/* ── Search ────────────────────────────────────────────────────── */
.toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
}
.toolbar input[type="search"] {
  width: 300px;
  max-width: 100%;
  padding: 7px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: .88rem;
  outline: none;
  transition: border-color .15s;
}
.toolbar input[type="search"]:focus { border-color: var(--accent); }
.toolbar .row-count { font-size: .82rem; color: var(--muted); }

/* ── Panel ─────────────────────────────────────────────────────── */
.tab-panel { display: none; }
.tab-panel.active { display: block; }

/* ── Table wrapper ─────────────────────────────────────────────── */
.table-wrap {
  background: var(--surface);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  overflow: auto;
  max-height: 60vh;
}

/* ── Table ─────────────────────────────────────────────────────── */
table {
  width: 100%;
  border-collapse: collapse;
  font-size: .83rem;
}

/* Filterable grids (main + today) share a <colgroup> so columns line up
   even when the today-grid has empty cells. table-layout: fixed makes
   the col widths authoritative — content doesn't override them. */
.filterable-table { table-layout: fixed; }

thead th {
  position: sticky;
  top: 0;
  z-index: 2;
  background: var(--primary);
  color: #fff;
  padding: 10px 12px;
  text-align: left;
  font-weight: 600;
  white-space: nowrap;
  user-select: none;
  cursor: pointer;
}
thead th:hover { background: var(--primary-h); }
thead th .sort-icon { margin-left: 4px; font-size: .68rem; opacity: .4; }
thead th.asc  .sort-icon { opacity: 1; }
thead th.desc .sort-icon { opacity: 1; }

tbody tr:nth-child(even) { background: var(--stripe); }
tbody tr:hover { background: var(--hover); }
tbody td {
  padding: 7px 12px;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ── Special cells ─────────────────────────────────────────────── */
.cell-price { color: var(--green); font-weight: 700; }

.cell-link a {
  display: inline-block;
  background: var(--accent);
  color: #fff;
  padding: 3px 11px;
  border-radius: 4px;
  text-decoration: none;
  font-size: .76rem;
  font-weight: 500;
  transition: background .15s;
}
.cell-link a:hover { background: var(--accent-h); }

.cell-vin {
  font-family: "SF Mono", "Fira Code", monospace;
  font-size: .78rem;
  color: var(--muted);
  letter-spacing: .03em;
}

.cell-model {
  max-width: 260px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: default;
}

/* ── Bidfax button ─────────────────────────────────────────────── */
.cell-bidfax a {
  display: inline-block;
  background: #d97706;
  color: #fff;
  padding: 3px 11px;
  border-radius: 4px;
  text-decoration: none;
  font-size: .76rem;
  font-weight: 500;
  transition: background .15s;
}
.cell-bidfax a:hover { background: #b45309; }

/* ── Empty / no-results ────────────────────────────────────────── */
tr.no-results td {
  text-align: center;
  padding: 28px;
  color: var(--muted);
  font-style: italic;
}

/* ── Today's auctions section ──────────────────────────────────── */
.today-section { margin-top: 28px; }
.today-section-header {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.today-section-header h3 {
  font-size: .9rem;
  font-weight: 700;
  color: var(--today-hd);
}
.today-badge {
  background: var(--today-hd);
  color: #fff;
  border-radius: 10px;
  padding: 1px 9px;
  font-size: .72rem;
  font-weight: 600;
}
.today-table thead th {
  background: var(--today-hd);
}
.today-table thead th:hover { background: #14532d; }
.today-table tbody tr:nth-child(even) { background: var(--today-bg); }

/* ── Mobile hamburger button (visible only on small screens) ───── */
.mobile-menu-btn {
  display: none;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 6px 12px;
  font-size: 1.1rem;
  cursor: pointer;
  line-height: 1;
}
.mobile-menu-btn:hover { background: var(--hover); border-color: var(--accent); }

/* ── Mobile layout ────────────────────────────────────────────── */
@media (max-width: 768px) {
  .container { padding: 12px; }
  header { padding: 12px 16px; }
  header h1 { font-size: 1.1rem; }

  /* Hide search box on mobile */
  .toolbar input[type="search"] { display: none; }
  .toolbar { justify-content: space-between; }

  /* Show hamburger; it toggles the model filter */
  .mobile-menu-btn { display: inline-block; }

  /* Summary + model filter start collapsed (JS strips [open] on load) */
  .summary-section, .model-filter { padding: 8px 12px; }

  /* ── Hamburger drawer menu ────────────────────────────────── */
  /* Tabs are hidden by default; opening the menu turns the strip into a
     vertical list so makes render as menu lines, not pill buttons. */
  .tab-strip { display: none; }
  body.menu-open .tab-strip {
    display: flex;
    flex-direction: column;
    gap: 0;
    margin-bottom: 12px;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: var(--surface);
    overflow: hidden;
  }
  body.menu-open .tab-btn {
    border: none;
    border-bottom: 1px solid var(--border);
    border-radius: 0;
    padding: 10px 14px;
    background: var(--surface);
    color: var(--text);
    text-align: left;
    font-weight: 500;
    white-space: nowrap;
  }
  body.menu-open .tab-btn:last-child { border-bottom: none; }
  body.menu-open .tab-btn.active {
    background: var(--accent);
    color: #fff;
  }
  body.menu-open .tab-btn:not(.active) .badge {
    background: var(--border);
    color: var(--muted);
  }

  /* ── Table → 2-row card layout ─────────────────────────────── */
  .table-wrap { max-height: none; overflow: visible; box-shadow: none; background: transparent; }
  .filterable-table, .filterable-table tbody { display: block; }
  .filterable-table thead { display: none; }

  /* Fixed Price/Link column widths make those lines up across cards.
     Odometer is placed directly under Price (shares col 2) so the two
     right-aligned values form a vertical pair. Model takes the remaining
     space, giving longer model names more room to breathe. */
  .filterable-table tbody tr {
    display: grid;
    grid-template-columns: 1fr 85px 70px;
    grid-template-areas:
      "model price link"
      "dmg   odo   .";
    gap: 6px 10px;
    align-items: center;
    padding: 10px 12px;
    margin-bottom: 8px;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: 0 1px 3px rgba(0,0,0,.04);
  }
  /* Zebra striping: odd cards white, even main-cards grey, even today-cards
     green — same palette as the desktop rows. The base rules (tbody tr
     nth-child(even), .today-table tbody tr:nth-child(even)) defined above
     cascade in automatically now that the mobile block doesn't override them. */
  .filterable-table tbody tr:nth-child(odd) { background: var(--surface); }
  .filterable-table tbody tr:hover { background: var(--hover); }

  .filterable-table tbody td {
    padding: 0;
    border-bottom: none;
    font-size: .82rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
  }

  /* Fields not shown on mobile cards */
  .filterable-table td[data-field="make"],
  .filterable-table td[data-field="fuel-type"],
  .filterable-table td[data-field="location"],
  .filterable-table td[data-field="auction-date"],
  .filterable-table td[data-field="acv"],
  .filterable-table td[data-field="year"],
  .filterable-table td[data-field="vin"],
  .filterable-table td[data-field="lot-number"] { display: none; }

  /* Row 1: Model (bold, flex) | Price (right) | Odometer (right) | Link (right) */
  .filterable-table td[data-field="model"] {
    grid-area: model;
    font-weight: 700;
    font-size: .95rem;
  }
  .filterable-table td[data-field="price"] {
    grid-area: price;
    text-align: right;
    font-size: .9rem;
  }
  .filterable-table td[data-field="odometer"] {
    grid-area: odo;
    text-align: right;
  }
  .filterable-table td[data-field="link"] {
    grid-area: link;
    justify-self: end;
  }

  /* Row 2: Damage (col 1, left) | Odometer sits directly under Price */
  .filterable-table td[data-field="primary-damage"] {
    grid-area: dmg;
    text-align: left;
  }

  /* Emoji labels on the fields that remain */
  .filterable-table td[data-field="odometer"]::before     { content: "🚗 "; }
  .filterable-table td[data-field="price"]::before        { content: "💲 "; }
  .filterable-table td[data-field="primary-damage"]::before { content: "⚠️ "; }

  .filterable-table td.cell-model { max-width: none; }

  /* Today's Auctions cards: no price (lots haven't been priced yet) */
  .today-table tbody td[data-field="price"] { display: none; }
}
"""

# ---------------------------------------------------------------------------
# JavaScript
# ---------------------------------------------------------------------------

JS = """\
(function () {
  /* ── Utilities ─────────────────────────────────────────────── */
  function activePanel() {
    return document.querySelector('.tab-panel.active');
  }

  function updateCount(panel) {
    var countEl = document.getElementById('row-count');
    if (!countEl || !panel) return;
    var mainTable = panel.querySelector('.main-table');
    if (!mainTable) return;
    var visible = Array.from(mainTable.querySelectorAll('tbody tr:not(.no-results)'))
                       .filter(function (r) { return r.style.display !== 'none'; }).length;
    countEl.textContent = visible + ' row(s)';
  }

  function getCheckedModels(panel) {
    var cbs = panel.querySelectorAll('.model-cb:not([data-all])');
    if (!cbs.length) return null;
    var checked = Array.from(cbs).filter(function (c) { return c.checked; });
    if (checked.length === cbs.length) return null;   /* all checked = no filter */
    return new Set(checked.map(function (c) { return c.value; }));
  }

  function applyFilters(panel) {
    if (!panel) return;
    var searchInput = document.getElementById('search-input');
    var q      = searchInput ? searchInput.value.trim().toLowerCase() : '';
    var models = getCheckedModels(panel);

    panel.querySelectorAll('.filterable-table').forEach(function (table) {
      var tbody = table.tBodies[0];
      if (!tbody) return;
      var visible = 0;
      Array.from(tbody.rows).forEach(function (tr) {
        if (tr.classList.contains('no-results')) return;
        var modelOk = !models || models.has(tr.dataset.model || '');
        var textOk  = !q     || tr.textContent.toLowerCase().includes(q);
        var show    = modelOk && textOk;
        tr.style.display = show ? '' : 'none';
        if (show) visible++;
      });
      var noRes = tbody.querySelector('.no-results');
      if (noRes) noRes.style.display = visible === 0 ? '' : 'none';
    });

    updateCount(panel);
  }

  /* ── Tab switching ─────────────────────────────────────────── */
  document.querySelectorAll('.tab-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.tab-btn').forEach(function (b) { b.classList.remove('active'); });
      document.querySelectorAll('.tab-panel').forEach(function (p) { p.classList.remove('active'); });
      btn.classList.add('active');
      var panel = document.getElementById(btn.dataset.target);
      panel.classList.add('active');
      var searchInput = document.getElementById('search-input');
      if (searchInput) searchInput.value = '';
      applyFilters(panel);
    });
  });

  /* ── Text search ───────────────────────────────────────────── */
  var searchInput = document.getElementById('search-input');
  if (searchInput) {
    searchInput.addEventListener('input', function () {
      applyFilters(activePanel());
    });
  }

  /* ── Model filter ──────────────────────────────────────────── */
  document.querySelectorAll('.model-filter').forEach(function (filter) {
    filter.addEventListener('change', function (e) {
      var cb    = e.target;
      var panel = filter.closest('.tab-panel');
      if (cb.hasAttribute('data-all')) {
        filter.querySelectorAll('.model-cb:not([data-all])').forEach(function (c) {
          c.checked = cb.checked;
        });
      } else {
        var all        = Array.from(filter.querySelectorAll('.model-cb:not([data-all])'));
        var allChecked = all.every(function (c) { return c.checked; });
        var allCb      = filter.querySelector('[data-all]');
        if (allCb) allCb.checked = allChecked;
      }
      applyFilters(panel);
    });
  });

  /* ── Column sorting ─────────────────────────────────────────── */
  function rawVal(td) {
    return td.dataset.raw !== undefined ? td.dataset.raw : td.textContent.trim();
  }
  function numVal(s) { return parseFloat(String(s).replace(/[$,]/g, '')) || 0; }

  document.querySelectorAll('thead th').forEach(function (th) {
    if (th.closest('table.no-sort')) return;
    var ascending = true;
    th.addEventListener('click', function () {
      var table  = th.closest('table');
      var idx    = Array.from(th.parentNode.children).indexOf(th);
      var isNum  = th.dataset.type === 'number';
      var tbody  = table.tBodies[0];
      var rows   = Array.from(tbody.rows).filter(function (r) {
        return !r.classList.contains('no-results');
      });

      rows.sort(function (a, b) {
        var av = rawVal(a.cells[idx]);
        var bv = rawVal(b.cells[idx]);
        if (isNum) return ascending ? numVal(av) - numVal(bv) : numVal(bv) - numVal(av);
        return ascending ? av.localeCompare(bv, undefined, {numeric: true})
                         : bv.localeCompare(av, undefined, {numeric: true});
      });
      rows.forEach(function (r) { tbody.appendChild(r); });

      table.querySelectorAll('thead th').forEach(function (t) {
        t.classList.remove('asc', 'desc');
        var icon = t.querySelector('.sort-icon');
        if (icon) icon.textContent = ' ⇅';
      });
      th.classList.add(ascending ? 'asc' : 'desc');
      var icon = th.querySelector('.sort-icon');
      if (icon) icon.textContent = ascending ? ' ▲' : ' ▼';
      ascending = !ascending;
    });
  });

  /* ── Default sort: Auction Date descending (newest first) ───── */
  function sortByAuctionDateDesc(table) {
    var headers = table.querySelectorAll('thead th');
    for (var i = 0; i < headers.length; i++) {
      var th = headers[i];
      var first = null;
      th.childNodes.forEach(function (n) {
        if (!first && n.nodeType === Node.TEXT_NODE && n.textContent.trim()) {
          first = n.textContent.trim();
        }
      });
      if (first === 'Auction Date') {
        th.click();   /* first click: asc */
        th.click();   /* second click: desc */
        return;
      }
    }
  }

  document.querySelectorAll('table.filterable-table').forEach(sortByAuctionDateDesc);

  /* ── Mobile: start with details collapsed + menu closed ─────── */
  var mobileMQ = window.matchMedia('(max-width: 768px)');
  if (mobileMQ.matches) {
    document.querySelectorAll('details.summary-section, details.model-filter')
      .forEach(function (d) { d.removeAttribute('open'); });
  }

  /* Hamburger toggles the whole mobile menu (make tabs + model filter) */
  var hamburger = document.getElementById('mobile-menu-btn');
  if (hamburger) {
    hamburger.addEventListener('click', function () {
      var open = document.body.classList.toggle('menu-open');
      /* When the menu opens, also expand the model filter so it's usable */
      var panel = activePanel();
      if (panel) {
        var filter = panel.querySelector('details.model-filter');
        if (filter) filter.open = open;
      }
    });
  }

  /* Tapping a tab on mobile closes the menu so the cards are visible again */
  document.querySelectorAll('.tab-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (mobileMQ.matches) {
        document.body.classList.remove('menu-open');
      }
    });
  });

  /* ── Init sort icons + first panel ──────────────────────────── */
  document.querySelectorAll('thead th .sort-icon').forEach(function (el) {
    if (!el.textContent) el.textContent = ' ⇅';
  });
  var firstPanel = document.querySelector('.tab-panel.active');
  if (firstPanel) applyFilters(firstPanel);
})();
"""

# ---------------------------------------------------------------------------
# Bidfax lookup (browser-based)
# ---------------------------------------------------------------------------

_BIDFAX_DOMAIN = "bidfax.info"


def _row_link(row: tuple, link_idx: int) -> str:
    """Extract the resolved URL from a row's Link cell."""
    if link_idx < 0 or link_idx >= len(row):
        return ""
    raw = str(row[link_idx] or "").strip()
    m = _HYPERLINK_RE.match(raw)
    return m.group(1) if m else raw


def _vins_needing_lookup(ws) -> set[str]:
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return set()
    headers  = [str(h or "") for h in rows[0]]
    if "VIN" not in headers:
        return set()
    vin_idx  = headers.index("VIN")
    link_idx = headers.index("Link") if "Link" in headers else -1
    result: set[str] = set()
    for row in rows[1:]:
        vin = str(row[vin_idx] or "").strip() if vin_idx < len(row) else ""
        if not vin or vin.upper() == "NONE":
            continue
        if _BIDFAX_DOMAIN not in _row_link(row, link_idx):
            result.add(vin)
    return result


def _collect_vins(wb: openpyxl.Workbook) -> set[str]:
    vins: set[str] = set()
    for name in wb.sheetnames:
        vins |= _vins_needing_lookup(wb[name])
    return vins


def _lookup_bidfax_urls(
    vins: set[str],
    cache_path: Path,
    delay: float,
    browser_port: int | None = None,
    client: bidfax.BidfaxClient | None = None,
) -> dict[str, str]:
    return bidfax.run_batch_vins(
        sorted(vins), delay, cache_path,
        browser_port=browser_port, client=client,
    )


# ---------------------------------------------------------------------------
# Today's lots loader
# ---------------------------------------------------------------------------

def _load_today_lots(search_dir: Path, today_str: str) -> dict[str, list[dict]]:
    """Load copart + iaai search CSVs for today. Returns {MAKE_UPPER: [row_dicts]}."""
    result: dict[str, list[dict]] = {}
    for auction in ("copart", "iaai"):
        path = search_dir / f"{auction}_search_{today_str}.csv"
        if not path.exists():
            continue
        with path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                make = str(row.get("Make", "") or "").strip().upper()
                if make:
                    result.setdefault(make, []).append(dict(row))
    return result


# ---------------------------------------------------------------------------
# HTML cell / row helpers
# ---------------------------------------------------------------------------

def _extract_url(value) -> str | None:
    m = _HYPERLINK_RE.match(str(value or ""))
    return m.group(1) if m else None


_MODEL_MAX_LEN = 35


def _field_slug(header: str) -> str:
    """Normalise 'Lot Number' -> 'lot-number' for the data-field CSS hook."""
    return re.sub(r"[^a-z0-9]+", "-", header.lower()).strip("-")


def _td_attrs(header: str, extra: str = "") -> str:
    """Build the common data-field / data-label attrs for a <td>."""
    slug = _field_slug(header)
    return f'data-field="{slug}" data-label="{_html.escape(header)}" {extra}'.strip()


def _model_cell_html(raw: str) -> str:
    attrs = _td_attrs("Model", 'class="cell-model"')
    if len(raw) <= _MODEL_MAX_LEN:
        return f'<td {attrs}>{_html.escape(raw)}</td>'
    return (f'<td {attrs} title="{_html.escape(raw)}">'
            f'{_html.escape(raw[:_MODEL_MAX_LEN])}…</td>')


def _link_cell_html(raw: str) -> str:
    url = _extract_url(raw) or (raw if raw.startswith("http") else "")
    if not url:
        return f"<td {_td_attrs('Link')}></td>"
    klass = "cell-bidfax" if _BIDFAX_DOMAIN in url else "cell-link"
    label = "Bidfax"      if _BIDFAX_DOMAIN in url else "View"
    attrs = _td_attrs("Link", f'class="{klass}"')
    return f'<td {attrs}><a href="{_html.escape(url)}" target="_blank">{label}</a></td>'


def _cell_html(header: str, value) -> str:
    raw = "" if value is None else str(value).strip()

    if header == "Model":
        return _model_cell_html(raw)
    if header == "Link":
        return _link_cell_html(raw)
    if header == "Price":
        attrs = _td_attrs(header, 'class="cell-price"')
        return f'<td {attrs}>{_html.escape(raw)}</td>'
    if header == "VIN":
        attrs = _td_attrs(header, 'class="cell-vin"')
        return f'<td {attrs}>{_html.escape(raw)}</td>'
    if header in _NUMERIC_COLS:
        numeric = re.sub(r"[^\d.]", "", raw) or "0"
        attrs   = _td_attrs(header)
        return f'<td {attrs} data-raw="{_html.escape(numeric)}">{_html.escape(raw)}</td>'
    return f'<td {_td_attrs(header)}>{_html.escape(raw)}</td>'


# Fixed column widths — applied via <colgroup> so the main grid and the
# "Today's Auctions" grid share exactly the same layout, even when one of
# the tables has empty columns (e.g. Today's auctions have no Price yet).
# Values sum to ~1460px; with table-layout: fixed + width: 100% the
# browser scales them proportionally to fit the viewport.
_COL_WIDTHS = {
    "Make":           "100px",
    "Model":          "180px",
    "Year":            "55px",
    "Odometer":        "80px",
    "Price":           "85px",
    "Fuel Type":       "80px",
    "Lot Number":     "100px",
    "Link":            "75px",
    "Auction Date":   "160px",
    "Location":       "140px",
    "Primary Damage": "140px",
    "VIN":            "160px",
    "ACV":            "110px",
}


def _colgroup_html(headers: list) -> str:
    """Shared column widths for main + today tables so columns line up."""
    cols = "".join(
        f'<col style="width: {_COL_WIDTHS.get(h, "auto")}">' for h in headers
    )
    return f"<colgroup>{cols}</colgroup>"


def _thead_html(headers: list) -> str:
    cells = "".join(
        f'<th data-type="{"number" if h in _NUMERIC_COLS else "text"}">'
        f'{_html.escape(h)}<span class="sort-icon"></span></th>'
        for h in headers
    )
    return f"<thead><tr>{cells}</tr></thead>"


def _resolve_link(raw_value, vin: str, vin_to_url: dict | None) -> str:
    url = _extract_url(str(raw_value or "")) or (
        str(raw_value or "").strip() if str(raw_value or "").strip().startswith("http") else ""
    )
    if _BIDFAX_DOMAIN in url:
        return url
    if vin and vin_to_url:
        bidfax_url = vin_to_url.get(vin, "")
        if bidfax_url:
            return bidfax_url
    return url


# ---------------------------------------------------------------------------
# Summary section
# ---------------------------------------------------------------------------

def _summary_section_html(data_rows, headers: list[str]) -> str:
    """Static summary table: Model | Count | Avg Price (above the main grid)."""
    if "Model" not in headers or "Price" not in headers:
        return ""
    model_idx = headers.index("Model")
    price_idx = headers.index("Price")

    groups: dict[str, dict] = {}
    for row in data_rows:
        model = _model_key(str(row[model_idx].value or "").strip())
        if not model:
            continue
        price_raw = str(row[price_idx].value or "").strip()
        if model not in groups:
            groups[model] = {"count": 0, "prices": []}
        groups[model]["count"] += 1
        m = _PRICE_RE.match(price_raw)
        if m:
            groups[model]["prices"].append(float(m.group(1).replace(",", "")))

    if not groups:
        return ""

    rows_html = ""
    for model in sorted(groups):
        g   = groups[model]
        avg = f"${sum(g['prices']) / len(g['prices']):,.0f}" if g["prices"] else "—"
        rows_html += (
            f"<tr>"
            f"<td>{_html.escape(model)}</td>"
            f"<td>{g['count']}</td>"
            f'<td class="cell-price">{avg}</td>'
            f"</tr>"
        )

    # Open by default; JS closes it on mobile screens.
    return (
        '<details class="summary-section" open>'
        '<summary class="summary-label">Summary by Model</summary>'
        '<table class="summary-table no-sort">'
        "<thead><tr><th>Model</th><th>Count</th><th>Avg&nbsp;Price</th></tr></thead>"
        f"<tbody>{rows_html}</tbody>"
        "</table></details>"
    )


# ---------------------------------------------------------------------------
# Model filter widget
# ---------------------------------------------------------------------------

def _model_filter_html(models: list[str]) -> str:
    if not models:
        return ""
    chips = []
    chips.append(
        '<label class="model-chip all-chip">'
        '<input type="checkbox" class="model-cb" data-all="1" checked> All</label>'
    )
    for m in models:
        e = _html.escape(m)
        chips.append(
            f'<label class="model-chip">'
            f'<input type="checkbox" class="model-cb" value="{e}" checked> {e}</label>'
        )
    # Open by default; JS closes it on mobile screens (hamburger button re-opens).
    return (
        '<details class="model-filter" open>'
        '<summary class="model-filter-label">Model</summary>'
        f'<div class="model-chips">{"".join(chips)}</div>'
        '</details>'
    )


# ---------------------------------------------------------------------------
# Table body builders
# ---------------------------------------------------------------------------

def _get_cell_value(h: str, row, src_idx: dict, vin: str, vin_to_url: dict | None):
    i   = src_idx.get(h, -1)
    raw = row[i].value if i >= 0 else None
    if h == "Link":
        return _resolve_link(raw, vin, vin_to_url)
    if h == _AUCTION_DATE_COL and raw:
        return _normalize_auction_date(str(raw).strip())
    return raw


def _tbody_html(data_rows, src_headers: list, vin_idx, vin_to_url: dict | None) -> str:
    src_idx   = {h: i for i, h in enumerate(src_headers)}
    model_idx = src_idx.get("Model", -1)

    parts = []
    for row in data_rows:
        vin   = str(row[vin_idx].value or "").strip() if vin_idx is not None else ""
        model = _model_key(str(row[model_idx].value or "").strip() if model_idx >= 0 else "")
        cells = "".join(_cell_html(h, _get_cell_value(h, row, src_idx, vin, vin_to_url)) for h in src_headers)
        model_attr = f' data-model="{_html.escape(model)}"' if model else ""
        parts.append(f"<tr{model_attr}>{cells}</tr>")

    n = len(src_headers)
    parts.append(f'<tr class="no-results" style="display:none"><td colspan="{n}">No matching rows.</td></tr>')
    return f"<tbody>{''.join(parts)}</tbody>"


def _today_tbody_html(today_rows: list[dict], headers: list[str]) -> str:
    """Render today's search CSV rows using the workbook headers (Price/VIN left empty)."""
    parts = []
    for row in today_rows:
        model = _model_key(str(row.get("Model", "") or "").strip())
        cells = []
        for h in headers:
            if h in ("Price", "VIN"):
                val = ""
            else:
                val = str(row.get(h, "") or "").strip()
                if h == _AUCTION_DATE_COL:
                    val = _normalize_auction_date(val)
            cells.append(_cell_html(h, val))
        model_attr = f' data-model="{_html.escape(model)}"' if model else ""
        parts.append(f"<tr{model_attr}>{''.join(cells)}</tr>")

    n = len(headers)
    parts.append(f'<tr class="no-results" style="display:none"><td colspan="{n}">No matching rows.</td></tr>')
    return f"<tbody>{''.join(parts)}</tbody>"


# ---------------------------------------------------------------------------
# Model key helper
# ---------------------------------------------------------------------------

def _model_key(model: str) -> str:
    """Return the first word of a model string for grouping/filtering.

    e.g. 'GLE 350 4MATIC' → 'GLE', 'CR-V HYBRID' → 'CR-V', 'Q5' → 'Q5'
    """
    return model.split()[0] if model.strip() else ""


# ---------------------------------------------------------------------------
# Today-only panel builder (no workbook)
# ---------------------------------------------------------------------------

def _today_only_panel_content(today_rows: list[dict]) -> tuple[str, int]:
    """Build a panel containing only today's-auction rows.

    Used for makes that have no workbook sheet yet (freshly added to filters/),
    and as the sole content when the whole workbook is missing. The grid is
    styled as the green 'Today's Auctions' section because every row in it is
    a today-auction lot — there's no historical priced data to put under a
    blue main grid.
    """
    if not today_rows:
        return "<p>No data.</p>", 0

    headers = list(today_rows[0].keys())

    models = sorted({
        _model_key(str(r.get("Model", "") or "").strip())
        for r in today_rows
        if _model_key(str(r.get("Model", "") or "").strip())
    })

    filter_html = _model_filter_html(models)

    parts = []
    for row in today_rows:
        model = _model_key(str(row.get("Model", "") or "").strip())
        cells = []
        for h in headers:
            val = str(row.get(h, "") or "").strip()
            if h == _AUCTION_DATE_COL:
                val = _normalize_auction_date(val)
            cells.append(_cell_html(h, val))
        model_attr = f' data-model="{_html.escape(model)}"' if model else ""
        parts.append(f"<tr{model_attr}>{''.join(cells)}</tr>")

    n = len(headers)
    parts.append(f'<tr class="no-results" style="display:none"><td colspan="{n}">No matching rows.</td></tr>')
    tbody = f"<tbody>{''.join(parts)}</tbody>"

    table = (
        f'<table class="filterable-table main-table today-table">'
        f"{_colgroup_html(headers)}"
        f"{_thead_html(headers)}"
        f"{tbody}"
        f"</table>"
    )

    today_section = (
        '<div class="today-section">'
        '<div class="today-section-header">'
        f'<h3>Today\'s Auctions</h3>'
        f'<span class="today-badge">{len(today_rows)}</span>'
        "</div>"
        f'<div class="table-wrap">{table}</div>'
        "</div>"
    )

    content = filter_html + today_section
    return content, len(today_rows)


# ---------------------------------------------------------------------------
# Per-panel builder
# ---------------------------------------------------------------------------

def _extract_models(data_rows, headers: list[str], today_rows: list[dict]) -> list[str]:
    models: set[str] = set()
    if "Model" in headers:
        idx = headers.index("Model")
        for row in data_rows:
            val = _model_key(str(row[idx].value or "").strip())
            if val:
                models.add(val)
    for row in today_rows:
        val = _model_key(str(row.get("Model", "") or "").strip())
        if val:
            models.add(val)
    return sorted(models)


def _ws_to_panel_content(
    ws,
    vin_to_url: dict | None,
    today_rows: list[dict],
) -> tuple[str, int]:
    """Build the full HTML content for one tab panel. Returns (html, row_count)."""
    rows = list(ws.iter_rows(values_only=False))
    if not rows:
        return "<p>No data.</p>", 0

    headers   = [str(c.value or "") for c in rows[0]]
    data_rows = rows[1:]
    vin_idx   = headers.index("VIN") if "VIN" in headers else None

    models = _extract_models(data_rows, headers, today_rows)

    filter_html  = _model_filter_html(models)
    summary_html = _summary_section_html(data_rows, headers)

    colgroup = _colgroup_html(headers)

    main_table = (
        f'<table class="filterable-table main-table">'
        f"{colgroup}"
        f"{_thead_html(headers)}"
        f"{_tbody_html(data_rows, headers, vin_idx, vin_to_url)}"
        f"</table>"
    )

    today_section = ""
    if today_rows:
        today_table = (
            f'<table class="filterable-table today-table">'
            f"{colgroup}"
            f"{_thead_html(headers)}"
            f"{_today_tbody_html(today_rows, headers)}"
            f"</table>"
        )
        today_section = (
            '<div class="today-section">'
            '<div class="today-section-header">'
            f'<h3>Today\'s Auctions</h3>'
            f'<span class="today-badge">{len(today_rows)}</span>'
            "</div>"
            f'<div class="table-wrap">{today_table}</div>'
            "</div>"
        )

    # Today's Auctions goes first — active lots the user is deciding on
    # right now are more useful than the historical grid underneath.
    content = (
        filter_html
        + summary_html
        + today_section
        + f'<div class="table-wrap">{main_table}</div>'
    )
    return content, len(data_rows)


# ---------------------------------------------------------------------------
# Full page builder
# ---------------------------------------------------------------------------

def _build_html(
    wb: openpyxl.Workbook | None,
    title: str,
    vin_to_url: dict | None,
    today_lots: dict[str, list[dict]],
) -> str:
    tab_btns = []
    panels   = []

    if wb is not None:
        for i, name in enumerate(wb.sheetnames):
            make_upper  = name.upper()
            today_rows  = today_lots.get(make_upper, [])
            panel_html, count = _ws_to_panel_content(wb[name], vin_to_url, today_rows)
            safe_id = re.sub(r"\W+", "_", name)
            active  = "active" if i == 0 else ""

            tab_btns.append(
                f'<button class="tab-btn {active}" data-target="{safe_id}">'
                f'{_html.escape(name)}<span class="badge">{count}</span></button>'
            )
            panels.append(
                f'<div class="tab-panel {active}" id="{safe_id}">'
                f"{panel_html}</div>"
            )

        # Makes that appear in today's auction CSV but not yet in the workbook
        # (e.g. a Make freshly added to filters/, no priced lots yet) would
        # otherwise have no tab. Render them as today-only panels so users
        # can see the new lots immediately, before any pricing pass runs.
        sheet_makes_upper = {n.upper() for n in wb.sheetnames}
        new_makes = sorted(m for m in today_lots if m not in sheet_makes_upper)
        for make in new_makes:
            today_rows = today_lots[make]
            panel_html, count = _today_only_panel_content(today_rows)
            safe_id = re.sub(r"\W+", "_", make)
            tab_btns.append(
                f'<button class="tab-btn" data-target="{safe_id}">'
                f'{_html.escape(make)}<span class="badge">{count}</span></button>'
            )
            panels.append(
                f'<div class="tab-panel" id="{safe_id}">{panel_html}</div>'
            )

        total      = sum(wb[n].max_row - 1 for n in wb.sheetnames)
        make_count = len(wb.sheetnames) + len(new_makes)
        subtitle   = f"{total} vehicle(s) &nbsp;·&nbsp; {make_count} make(s)"
    else:
        for i, make in enumerate(sorted(today_lots.keys())):
            today_rows = today_lots[make]
            panel_html, count = _today_only_panel_content(today_rows)
            safe_id = re.sub(r"\W+", "_", make)
            active  = "active" if i == 0 else ""

            tab_btns.append(
                f'<button class="tab-btn {active}" data-target="{safe_id}">'
                f'{_html.escape(make)}<span class="badge">{count}</span></button>'
            )
            panels.append(
                f'<div class="tab-panel {active}" id="{safe_id}">'
                f"{panel_html}</div>"
            )

        total_today = sum(len(v) for v in today_lots.values())
        subtitle    = f"{total_today} vehicle(s) &nbsp;·&nbsp; {len(today_lots)} make(s) — Today's lots only"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_html.escape(title)}</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
<header>
  <h1>{_html.escape(title)}</h1>
  <span class="subtitle">{subtitle}</span>
</header>
<div class="container">
  <div class="tab-strip">{"".join(tab_btns)}</div>
  <div class="toolbar">
    <button id="mobile-menu-btn" class="mobile-menu-btn" aria-label="Toggle model filter">☰</button>
    <input id="search-input" type="search" placeholder="Filter visible table…">
    <span class="row-count" id="row-count"></span>
  </div>
  {"".join(panels)}
</div>
<script src="script.js"></script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    today_default = date.today().strftime("%Y_%m_%d")

    parser = argparse.ArgumentParser(
        description="Generate an HTML report from the auction results workbook.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--workbook",     "-w", default="auction_results.xlsx",
                        help="Source workbook (default: auction_results.xlsx)")
    parser.add_argument("--out",          "-o", default="html_report",
                        help="Output folder (default: html_report)")
    parser.add_argument("--title",        "-t", default="Auction Results",
                        help="Page title (default: Auction Results)")
    parser.add_argument("--search-dir",   "-s", default=None,
                        help="Directory with today's search CSVs (default: workbook directory)")
    parser.add_argument("--today-date",         default=today_default,
                        help=f"Date of today's search files yyyy_mm_dd (default: {today_default})")
    parser.add_argument("--no-bidfax",          action="store_true",
                        help="Skip Bidfax VIN lookup")
    parser.add_argument("--bidfax-cache",       default="bidfax_cache.json",
                        help="Cache file for bidfax lookups (default: bidfax_cache.json)")
    parser.add_argument("--bidfax-delay",  type=float, default=2.0,
                        help="Seconds between bidfax requests (default: 2.0)")
    parser.add_argument("--browser-port", type=int, default=None,
                        help="Connect to a running Chrome on this port instead of launching one")
    args = parser.parse_args()

    workbook_path = Path(args.workbook)
    search_dir    = Path(args.search_dir) if args.search_dir else workbook_path.parent

    out_dir = Path(args.out)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    print(f"[*] Output folder : {out_dir.resolve()}")

    if not workbook_path.exists():
        print(f"[!] Workbook not found: {workbook_path} — skipping workbook conversion")
        wb         = None
        vin_to_url = None
    else:
        print(f"[*] Loading       : {workbook_path}")
        wb         = openpyxl.load_workbook(workbook_path)
        vins       = _collect_vins(wb)
        cache_path = Path(args.bidfax_cache)
        vin_to_url = None if args.no_bidfax else _lookup_bidfax_urls(
            vins, cache_path, args.bidfax_delay, browser_port=args.browser_port
        )

    (out_dir / "style.css").write_text(CSS, encoding="utf-8")
    (out_dir / "script.js").write_text(JS,  encoding="utf-8")
    print("[+] style.css  written")
    print("[+] script.js  written")

    print(f"[*] Loading today's lots from: {search_dir} (date: {args.today_date})")
    today_lots  = _load_today_lots(search_dir, args.today_date)
    total_today = sum(len(v) for v in today_lots.values())
    print(f"[*] Today's lots  : {total_today} across {len(today_lots)} make(s)")

    html_content = _build_html(wb, args.title, vin_to_url, today_lots)
    (out_dir / "index.html").write_text(html_content, encoding="utf-8")
    sheets_info = f"{len(wb.sheetnames)} sheet(s)" if wb is not None else "today-only"
    print(f"[+] index.html written  ({sheets_info})")
    print(f"\n[+] Done → {(out_dir / 'index.html').resolve()}")


if __name__ == "__main__":
    main()
