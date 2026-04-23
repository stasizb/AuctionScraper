"""Workbook (openpyxl) helpers shared between price_refresh and price_fix."""

from __future__ import annotations

from dataclasses import dataclass

from core.columns import LINK_COL, LOT_COL, PRICE_COL, VIN_COL


@dataclass(frozen=True)
class ColumnIndices:
    """1-based cell indices for the columns of interest in one sheet.

    `lot` is mandatory; the others are None when the sheet is missing that column.
    """
    lot:   int
    price: int | None
    vin:   int | None
    link:  int | None


def resolve_columns(headers: list) -> ColumnIndices | None:
    """Return 1-based indices for LOT/PRICE/VIN/LINK, or None if LOT is missing."""
    if LOT_COL not in headers:
        return None
    return ColumnIndices(
        lot   =  headers.index(LOT_COL)   + 1,
        price = (headers.index(PRICE_COL) + 1) if PRICE_COL in headers else None,
        vin   = (headers.index(VIN_COL)   + 1) if VIN_COL   in headers else None,
        link  = (headers.index(LINK_COL)  + 1) if LINK_COL  in headers else None,
    )


def apply_result_to_row(row, cols: ColumnIndices, price: str, vin: str, url: str) -> None:
    """Apply a (price, vin, url) result to one openpyxl row tuple.

    Empty `vin` / `url` leave the existing cell alone. `Link` is always written
    as an =HYPERLINK(...) formula so workbook formatting stays consistent.
    """
    if cols.price:
        row[cols.price - 1].value = price
    if cols.vin and vin:
        row[cols.vin - 1].value = vin
    if cols.link and url:
        new_val = f'=HYPERLINK("{url}")'
        if str(row[cols.link - 1].value or "") != new_val:
            row[cols.link - 1].value = new_val
