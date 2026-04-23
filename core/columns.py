"""Canonical column names and sentinel values used across all CSVs / workbook sheets.

Every script that touches the CSV/workbook schema should import from here —
don't duplicate these string literals anywhere else. If the schema evolves,
change it in one place.
"""

# CSV / workbook column names
LOT_COL    = "Lot Number"
PRICE_COL  = "Price"
VIN_COL    = "VIN"
LINK_COL   = "Link"
MAKE_COL   = "Make"
MODEL_COL  = "Model"

# Price sentinel meaning "the bidfax lookup hasn't returned a final price yet"
IN_PROGRESS = "In Progress"
