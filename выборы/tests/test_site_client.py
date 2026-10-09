import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx

from election_parser.site_client import get_json, official_client


class SessionRefreshTests(unittest.TestCase):
    def test_failed_request_reports_http_status(self) -> None:
        with httpx.Client(base_url="http://example.test",
                          transport=httpx.MockTransport(lambda _request: httpx.Response(404))) as client:
            with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
                get_json(client, "/reports/453", {"date": "2026-09-18"})

    def test_parallel_expired_requests_share_one_refresh(self) -> None:
        class Driver:
            def set_page_load_timeout(self, _timeout):
                pass

            def execute_cdp_cmd(self, _command, _params):
                pass

            def quit(self):
                pass

        with patch("election_parser.site_client.webdriver.Chrome", return_value=Driver()), \
             patch("election_parser.site_client._open_portal_headers",
                   side_effect=({"x-api-key": "old-key"}, {"x-api-key": "new-key"})) as headers:
            with official_client() as client:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    list(executor.map(client._refresh_session, ["old-key", "old-key"]))
                self.assertEqual(client.headers["x-api-key"], "new-key")
                self.assertEqual(client._session_refresh_count, 1)
        self.assertEqual(headers.call_count, 2)

    def test_refreshes_session_after_unauthorized_response(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                requests = []

                def handler(request):
                    requests.append(request.headers.get("x-api-key"))
                    if request.headers.get("x-api-key") == "new-key":
                        return httpx.Response(200, json={"ok": True})
                    return httpx.Response(status, json={"error": "expired"})

                with httpx.Client(base_url="http://example.test", headers={"x-api-key": "old-key"},
                                  transport=httpx.MockTransport(handler)) as client:
                    refreshes = []

                    def refresh(observed_key):
                        refreshes.append(observed_key)
                        client.headers["x-api-key"] = "new-key"

                    client._refresh_session = refresh
                    result = get_json(client, "/reports/453", {"date": "2026-09-18"})
                self.assertEqual(result, {"ok": True})
                self.assertEqual(requests, ["old-key", "new-key"])
                self.assertEqual(refreshes, ["old-key"])


if __name__ == "__main__":
    unittest.main()
