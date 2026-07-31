import unittest

from tests._helpers import ROOT  # noqa: F401
from clients.copart_session import build_session


class TestCopartSessionHeaders(unittest.TestCase):
    def test_browser_identity_cookies_and_csrf_are_preserved(self):
        ua = "Mozilla/5.0 (X11; Linux x86_64) Chrome/131.0 Safari/537.36"
        session = build_session([
            {"name": "incap_ses", "value": "opaque-cookie", "domain": ".copart.com", "path": "/"},
            {"name": "XSRF-TOKEN", "value": "opaque-xsrf", "domain": ".copart.com", "path": "/"},
        ], "opaque-xsrf", ua)
        self.assertEqual(session.headers["User-Agent"], ua)
        self.assertEqual(session.headers["Origin"], "https://www.copart.com")
        self.assertEqual(session.headers["Referer"], "https://www.copart.com/lotSearchResults/")
        self.assertEqual(session.headers["X-XSRF-TOKEN"], "opaque-xsrf")
        self.assertEqual(session.headers["sec-ch-ua-platform"], '"Linux"')
        self.assertEqual(session.cookies.get("incap_ses"), "opaque-cookie")


if __name__ == "__main__":
    unittest.main()
