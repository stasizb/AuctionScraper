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
        self.client = create_app(FakeLookup(), token="test-secret").test_client()

    @property
    def auth_headers(self):
        return {"Authorization": "Bearer test-secret"}

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    def test_search(self):
        response = self.client.get(
            "/search?q=12345678&auction=copart", headers=self.auth_headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["auction"], "copart")

    def test_search_requires_valid_query(self):
        response = create_app(token="test-secret").test_client().get(
            "/search?q=bad", headers=self.auth_headers
        )
        self.assertEqual(response.status_code, 400)

    def test_search_rejects_missing_token(self):
        response = self.client.get("/search?q=12345678")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {"error": "unauthorized"})

    def test_search_rejects_wrong_token(self):
        response = self.client.get(
            "/search?q=12345678",
            headers={"Authorization": "Bearer wrong-secret"},
        )
        self.assertEqual(response.status_code, 401)

    def test_search_rejects_non_bearer_scheme(self):
        response = self.client.get(
            "/search?q=12345678",
            headers={"Authorization": "Basic test-secret"},
        )
        self.assertEqual(response.status_code, 401)

    def test_missing_server_token_denies_search(self):
        response = create_app(FakeLookup(), token="").test_client().get(
            "/search?q=12345678",
            headers={"Authorization": "Bearer test-secret"},
        )
        self.assertEqual(response.status_code, 401)
