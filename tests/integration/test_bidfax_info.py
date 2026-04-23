"""Integration tests for scripts/bidfax_info.py driven by FakeBidfaxClient."""

import csv
import shutil
import tempfile
import unittest
from pathlib import Path

from tests._helpers import CSV_FIXTURES, ROOT  # noqa: F401

import bidfax_info
from clients.bidfax import FakeBidfaxClient


class TestBidfaxInfoCopart(unittest.TestCase):
    def setUp(self):
        self._tmp     = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)
        shutil.copy(CSV_FIXTURES / "copart_search_2026_01_02.csv", self.work_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_copart_flow_writes_price_csv_with_deletion(self):
        """Lot marked as NOT sale-ended gets deleted from input + output."""
        fake = FakeBidfaxClient(
            responses={
                "11111111": ("$18,500", "VIN111", "https://bidfax.info/honda/cr-v/one-vin-vin111.html"),
                "22222222": ("$20,000", "VIN222", "https://bidfax.info/honda/cr-v/two-vin-vin222.html"),
                # 33333333 has no response — will appear as In Progress
            },
            sale_ended={
                "https://www.copart.com/lot/11111111/honda-cr-v": True,
                "https://www.copart.com/lot/22222222/honda-cr-v": True,
                "https://www.copart.com/lot/33333333/audi-q5":    False,  # rescheduled
            },
        )
        cache_path = self.work_dir / "cache.json"
        log_path   = self.work_dir / "deletions.json"
        input_path = self.work_dir / "copart_search_2026_01_02.csv"
        out_path   = self.work_dir / "copart_price_2026_01_02.csv"

        bidfax_info.process(
            input_path, out_path, cache_path,
            delay=0, auction="copart", log_path=log_path,
            client=fake,
        )

        # Output CSV: 2 rows (the rescheduled lot is removed)
        with out_path.open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 2)
        by_lot = {r["Lot Number"]: r for r in rows}
        self.assertEqual(by_lot["11111111"]["Price"], "$18,500")
        self.assertEqual(by_lot["11111111"]["VIN"],   "VIN111")
        self.assertIn("bidfax.info", by_lot["11111111"]["Link"])
        self.assertEqual(by_lot["22222222"]["Price"], "$20,000")

        # Rescheduled lot removed from input CSV (in place)
        with input_path.open() as fh:
            input_rows = list(csv.DictReader(fh))
        input_lots = {r["Lot Number"] for r in input_rows}
        self.assertNotIn("33333333", input_lots)

        # Deletion log contains the removed row
        self.assertTrue(log_path.exists())

        # Cache contains only the final-price lots
        import json
        cache = json.loads(cache_path.read_text())
        self.assertIn("11111111", cache)
        self.assertIn("22222222", cache)
        self.assertNotIn("33333333", cache)

    def test_copart_second_run_uses_cache(self):
        """With a warm cache, the fake client is NOT called again."""
        fake = FakeBidfaxClient(
            responses={
                "11111111": ("$18,500", "VIN111", "https://bidfax.info/honda/cr-v/one-vin-vin111.html"),
                "22222222": ("$20,000", "VIN222", "https://bidfax.info/honda/cr-v/two-vin-vin222.html"),
            },
            sale_ended={
                "https://www.copart.com/lot/11111111/honda-cr-v": True,
                "https://www.copart.com/lot/22222222/honda-cr-v": True,
                "https://www.copart.com/lot/33333333/audi-q5":    False,
            },
        )
        cache_path = self.work_dir / "cache.json"
        log_path   = self.work_dir / "deletions.json"
        input_path = self.work_dir / "copart_search_2026_01_02.csv"
        out_path   = self.work_dir / "copart_price_2026_01_02.csv"

        bidfax_info.process(input_path, out_path, cache_path,
                            delay=0, auction="copart", log_path=log_path, client=fake)

        # Second run: warm cache — the two cached lots must not be re-looked up
        shutil.copy(CSV_FIXTURES / "copart_search_2026_01_02.csv", input_path)  # restore
        fake2 = FakeBidfaxClient()  # empty — would return In Progress if consulted
        bidfax_info.process(input_path, out_path, cache_path,
                            delay=0, auction="copart", log_path=log_path, client=fake2)

        self.assertNotIn("11111111", fake2.lookup_calls)
        self.assertNotIn("22222222", fake2.lookup_calls)
        # And sale-ended was not re-called for the cached lots
        self.assertNotIn("https://www.copart.com/lot/11111111/honda-cr-v", fake2.sale_ended_calls)
        self.assertNotIn("https://www.copart.com/lot/22222222/honda-cr-v", fake2.sale_ended_calls)


class TestBidfaxInfoIaai(unittest.TestCase):
    def setUp(self):
        self._tmp     = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)
        shutil.copy(CSV_FIXTURES / "iaai_search_2026_01_02.csv", self.work_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_iaai_flow_no_sale_ended_check(self):
        """IAAI path runs lookup_many directly, no sale-ended precheck."""
        fake = FakeBidfaxClient(responses={
            "44444444": ("$22,000", "VIN444", "https://bidfax.info/honda/cr-v/four-vin-vin444.html"),
            "55555555": ("$19,000", "VIN555", "https://bidfax.info/mazda/cx-5/five-vin-vin555.html"),
        })
        cache_path = self.work_dir / "cache.json"
        log_path   = self.work_dir / "deletions.json"
        input_path = self.work_dir / "iaai_search_2026_01_02.csv"
        out_path   = self.work_dir / "iaai_price_2026_01_02.csv"

        bidfax_info.process(input_path, out_path, cache_path,
                            delay=0, auction="iaai", log_path=log_path, client=fake)

        with out_path.open() as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 2)
        prices = {r["Lot Number"]: r["Price"] for r in rows}
        self.assertEqual(prices, {"44444444": "$22,000", "55555555": "$19,000"})
        # No sale-ended check for IAAI
        self.assertEqual(fake.sale_ended_calls, [])


if __name__ == "__main__":
    unittest.main()
