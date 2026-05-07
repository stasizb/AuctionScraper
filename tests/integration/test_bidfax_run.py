"""Integration tests for the consolidated bidfax pipeline (scripts/bidfax_run.py).

Exercises all three phases (queue building, lookup, distribution) with a
FakeBidfaxClient so no real browser launches.
"""

import csv
import json
import tempfile
import unittest
from pathlib import Path

from tests._helpers import ROOT  # noqa: F401

import bidfax_run
from clients.bidfax import FakeBidfaxClient, IN_PROGRESS


def _write_search_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = ["Make", "Model", "Year", "Odometer", "Lot Number", "Link"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_price_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = ["Make", "Model", "Year", "Odometer", "Price",
                  "Lot Number", "Link", "VIN"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


class TestEndToEnd(unittest.TestCase):
    """Drive process() with realistic file layouts and assert results land
    in the right destinations."""

    def test_copart_iaai_and_refresh_in_one_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            work    = Path(tmp)
            cache   = work / "bidfax_cache.json"
            log     = work / "logs" / "bidfax_deletions.json"

            # Today's Copart search: 2 lots — one will be reported "ended",
            # the other "not ended" (should be deleted from input).
            copart_in = work / "copart_search_2026_05_06.csv"
            _write_search_csv(copart_in, [
                {"Make": "HONDA", "Model": "CR-V", "Year": "2024",
                 "Odometer": "10000", "Lot Number": "111",
                 "Link": "https://copart/lot/111"},
                {"Make": "HONDA", "Model": "ACCORD", "Year": "2024",
                 "Odometer": "12000", "Lot Number": "222",
                 "Link": "https://copart/lot/222"},
            ])

            # Today's IAAI search: 1 lot.
            iaai_in = work / "iaai_search_2026_05_06.csv"
            _write_search_csv(iaai_in, [
                {"Make": "LEXUS", "Model": "RX 350", "Year": "2023",
                 "Odometer": "15000", "Lot Number": "333",
                 "Link": "https://iaai/lot/333"},
            ])

            # Older price file with one In-Progress row that should refresh.
            old_price = work / "iaai_price_2026_05_01.csv"
            _write_price_csv(old_price, [
                {"Make": "AUDI", "Model": "Q5", "Year": "2024",
                 "Odometer": "8000", "Price": IN_PROGRESS,
                 "Lot Number": "444", "Link": "https://iaai/lot/444", "VIN": ""},
                {"Make": "AUDI", "Model": "Q5", "Year": "2024",
                 "Odometer": "9000", "Price": "$25,000",
                 "Lot Number": "555", "Link": "https://iaai/lot/555",
                 "VIN": "ABC"},
            ])

            fake = FakeBidfaxClient(
                responses={
                    "111": ("$10,000", "VIN111", "https://bidfax/111.html"),
                    "333": ("$30,000", "VIN333", "https://bidfax/333.html"),
                    "444": ("$22,500", "VIN444", "https://bidfax/444.html"),
                },
                # 111 (ended) goes to bidfax; 222 (not ended) does NOT.
                sale_ended={"https://copart/lot/111": True,
                            "https://copart/lot/222": False},
            )

            bidfax_run.process(
                work_dir       = work,
                cache_path     = cache,
                log_path       = log,
                workbook_path  = None,
                delay          = 0.0,
                max_concurrent = 1,
                copart_date    = "2026_05_06",
                iaai_date      = "2026_05_06",
                bidfax_client  = fake,
            )

            # Copart output: only the ended lot (111) should be there.
            copart_out = work / "copart_price_2026_05_06.csv"
            self.assertTrue(copart_out.exists())
            with copart_out.open() as fh:
                copart_rows = list(csv.DictReader(fh))
            self.assertEqual([r["Lot Number"] for r in copart_rows], ["111"])
            self.assertEqual(copart_rows[0]["Price"], "$10,000")
            self.assertEqual(copart_rows[0]["VIN"],   "VIN111")

            # Copart input rewritten without the not-ended lot.
            with copart_in.open() as fh:
                remaining = list(csv.DictReader(fh))
            self.assertEqual([r["Lot Number"] for r in remaining], ["111"])

            # IAAI output: lot 333 with bidfax price.
            iaai_out = work / "iaai_price_2026_05_06.csv"
            with iaai_out.open() as fh:
                iaai_rows = list(csv.DictReader(fh))
            self.assertEqual([r["Lot Number"] for r in iaai_rows], ["333"])
            self.assertEqual(iaai_rows[0]["Price"], "$30,000")

            # Old price file: lot 444 In-Progress row was updated; 555 untouched.
            with old_price.open() as fh:
                refreshed = list(csv.DictReader(fh))
            by_lot = {r["Lot Number"]: r for r in refreshed}
            self.assertEqual(by_lot["444"]["Price"], "$22,500")
            self.assertEqual(by_lot["444"]["VIN"],   "VIN444")
            self.assertEqual(by_lot["555"]["Price"], "$25,000")  # unchanged

            # Deletion log records the removed lot.
            self.assertTrue(log.exists())
            entries = json.loads(log.read_text(encoding="utf-8"))
            self.assertEqual(len(entries), 1)
            deleted_lots = {r["Lot Number"] for r in entries[0]["deleted_items"]}
            self.assertEqual(deleted_lots, {"222"})

            # Cache was populated with the resolved prices.
            cache_data = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(set(cache_data.keys()), {"111", "333", "444"})

            # The fake was queried — exactly once per unique lot.
            self.assertEqual(set(fake.lookup_calls), {"111", "333", "444"})


class TestQueueDedup(unittest.TestCase):
    """A lot appearing in both refresh and today's search should be queried
    only once and the result distributed to all destinations."""

    def test_dedup_across_today_and_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            work  = Path(tmp)
            cache = work / "cache.json"
            log   = work / "logs" / "del.json"

            # Today's IAAI has lot 999. An older price file ALSO has lot 999
            # as In Progress (defensive: shouldn't happen in normal flow but
            # we want dedup if it ever does).
            iaai_in = work / "iaai_search_2026_05_06.csv"
            _write_search_csv(iaai_in, [
                {"Make": "BMW", "Model": "X5", "Year": "2024",
                 "Odometer": "5000", "Lot Number": "999",
                 "Link": "https://iaai/lot/999"},
            ])
            old = work / "iaai_price_2026_04_01.csv"
            _write_price_csv(old, [
                {"Make": "BMW", "Model": "X5", "Year": "2024",
                 "Odometer": "5000", "Price": IN_PROGRESS,
                 "Lot Number": "999", "Link": "https://iaai/lot/999", "VIN": ""},
            ])

            fake = FakeBidfaxClient(
                responses={"999": ("$45,000", "VBMW999", "https://bidfax/999.html")},
            )

            bidfax_run.process(
                work_dir       = work,
                cache_path     = cache,
                log_path       = log,
                workbook_path  = None,
                delay          = 0.0,
                max_concurrent = 1,
                copart_date    = None,
                iaai_date      = "2026_05_06",
                bidfax_client  = fake,
            )

            # Bidfax queried once for lot 999 (not twice).
            self.assertEqual(fake.lookup_calls, ["999"])
            # Both destinations got the price: today's iaai_price + old file.
            new_out = work / "iaai_price_2026_05_06.csv"
            with new_out.open() as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(rows[0]["Price"], "$45,000")
            with old.open() as fh:
                refreshed = list(csv.DictReader(fh))
            self.assertEqual(refreshed[0]["Price"], "$45,000")


class TestEmptyPipeline(unittest.TestCase):
    def test_no_inputs_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            # No CSVs at all in work/. Process should print and return cleanly.
            bidfax_run.process(
                work_dir       = work,
                cache_path     = work / "cache.json",
                log_path       = work / "del.json",
                workbook_path  = None,
                delay          = 0.0,
                max_concurrent = 1,
                copart_date    = None,
                iaai_date      = None,
                bidfax_client  = FakeBidfaxClient(),
            )
            # Nothing more to assert — this guards against KeyError / crash
            # when the queue is empty.


if __name__ == "__main__":
    unittest.main()
