import unittest

from tests._helpers import ROOT  # noqa: F401
from api import create_app
from clients.auction_lookup import normalize_query


class FakeLookup:
    def search(self, query, auction):
        return {"query": query, "query_type": "lot", "count": 0,
                "results": [], "errors": {}, "auction": auction}


class TestQueryValidation(unittest.TestCase):
    def test_vin(self):
        self.assertEqual(normalize_query(" 1hgcm82633a004352 "),
                         ("1HGCM82633A004352", "vin"))

    def test_lot(self):
        self.assertEqual(normalize_query("12345678"), ("12345678", "lot"))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            normalize_query("not-a-vin")


class TestAPI(unittest.TestCase):
    def setUp(self):
        self.client = create_app(FakeLookup()).test_client()

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    def test_search(self):
        response = self.client.get("/search?q=12345678&auction=copart")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["auction"], "copart")

    def test_search_requires_valid_query(self):
        response = create_app().test_client().get("/search?q=bad")
        self.assertEqual(response.status_code, 400)
