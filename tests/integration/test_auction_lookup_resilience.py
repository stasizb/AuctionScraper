import unittest
from unittest.mock import patch

from tests._helpers import ROOT  # noqa: F401
from api import create_app
from clients.auction_lookup import AuctionLookup
from clients.copart_session import CopartBlockedError


class FakeResponse:
    def __init__(self, status_code=200, lots=None):
        self.status_code = status_code
        self._lots = lots or []

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return {"data": {"results": {"content": self._lots}}}


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.response


class PayloadLookup:
    def __init__(self, payload):
        self.payload = payload

    def search(self, query, auction):
        return {**self.payload, "query": query, "query_type": "vin"}


class TestCopartLookup(unittest.TestCase):
    def test_copart_works_without_browser_fallback(self):
        browser_calls = []
        lookup = AuctionLookup(browser_search=lambda *a, **k: browser_calls.append(a))
        lookup._sessions["copart"] = FakeSession(FakeResponse(200, [{"ln": "123"}]))
        rows = lookup.search_copart("1HGCM82633A004352")
        self.assertEqual(rows[0]["lot_number"], "123")
        self.assertEqual(browser_calls, [])

    def test_copart_403_uses_browser_fallback(self):
        lookup = AuctionLookup(browser_search=lambda query, headless: [
            {"ln": "456", "fv": query, "mkn": "HONDA"}
        ])
        lookup._sessions["copart"] = FakeSession(FakeResponse(403))
        rows = lookup.search_copart("1HGCM82633A004352")
        self.assertEqual(rows[0]["lot_number"], "456")
        self.assertEqual(rows[0]["auction"], "copart")

    def test_copart_403_and_blocked_browser_is_structured(self):
        def blocked(*_args, **_kwargs):
            raise CopartBlockedError("Copart blocked the Railway/browser network address (HTTP 403)")

        lookup = AuctionLookup(browser_search=blocked)
        lookup._sessions["copart"] = FakeSession(FakeResponse(403))
        client = create_app(lookup, token="secret").test_client()
        response = client.get(
            "/search?q=1HGCM82633A004352&auction=copart",
            headers={"Authorization": "Bearer secret"},
        )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.get_json()["errors"]["copart"],
            "Copart blocked the Railway/browser network address (HTTP 403)",
        )
        self.assertEqual(response.content_type, "application/json")


class TestPartialApiResponses(unittest.TestCase):
    def request(self, payload):
        return create_app(PayloadLookup(payload), token="secret").test_client().get(
            "/search?q=1HGCM82633A004352&auction=all",
            headers={"Authorization": "Bearer secret"},
        )

    def test_iaai_result_survives_copart_error(self):
        response = self.request({
            "count": 1,
            "results": [{"auction": "iaai", "lot_number": "789"}],
            "errors": {"copart": "Copart blocked (HTTP 403)"},
        })
        self.assertEqual(response.status_code, 207)
        self.assertEqual(response.get_json()["results"][0]["auction"], "iaai")
        self.assertIn("copart", response.get_json()["errors"])

    def test_both_sources_unavailable_is_still_structured(self):
        response = self.request({
            "count": 0,
            "results": [],
            "errors": {"copart": "blocked", "iaai": "unavailable"},
        })
        self.assertEqual(response.status_code, 207)
        self.assertEqual(response.get_json()["results"], [])
        self.assertEqual(set(response.get_json()["errors"]), {"copart", "iaai"})

    def test_successful_all_sources_response_is_200(self):
        response = self.request({"count": 0, "results": [], "errors": {}})
        self.assertEqual(response.status_code, 200)

    def test_real_aggregator_keeps_iaai_when_copart_fails(self):
        lookup = AuctionLookup()
        iaai_row = {
            "auction": "iaai", "lot_number": "789", "vin": "1HGCM82633A004352"
        }
        with patch.object(lookup, "search_copart", side_effect=CopartBlockedError("blocked")), \
             patch.object(lookup, "search_iaai", return_value=[iaai_row]):
            payload = lookup.search("1HGCM82633A004352", "all")
        self.assertEqual(payload["results"], [iaai_row])
        self.assertEqual(payload["errors"], {"copart": "blocked"})

    def test_real_aggregator_reports_both_source_failures(self):
        lookup = AuctionLookup()
        with patch.object(lookup, "search_copart", side_effect=CopartBlockedError("blocked")), \
             patch.object(lookup, "search_iaai", side_effect=RuntimeError("private detail")):
            payload = lookup.search("1HGCM82633A004352", "all")
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["errors"]["copart"], "blocked")
        self.assertEqual(payload["errors"]["iaai"], "IAAI source unavailable (RuntimeError)")
        self.assertNotIn("private detail", str(payload))


if __name__ == "__main__":
    unittest.main()
