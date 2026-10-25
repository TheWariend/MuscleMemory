import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from parceldesk.server import create_server


class TestHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.server = create_server(Path(cls.directory.name) / "http.sqlite", 0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.directory.cleanup()

    def request(self, path, data=None, headers=None):
        request = Request(
            self.base + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.read(), response.headers

    def test_dashboard_serves_the_warehouse_interface(self):
        status, body, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Retries happen. Stock stays right.", body)
        self.assertIn("text/html", headers["Content-Type"])

    def test_api_retries_return_duplicate_and_preserve_one_movement(self):
        event = {"id": "http-retry", "kind": "received", "sku": "MUG-02", "quantity": 2}
        origin = {"Origin": self.base}
        status, body, _ = self.request("/api/events", event, origin)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "processed")
        status, body, _ = self.request("/api/events", event, origin)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "duplicate")
        _, body, _ = self.request("/api/state")
        rows = [row for row in json.loads(body)["movements"] if row["event_id"] == "http-retry"]
        self.assertEqual(len(rows), 1)

    def test_changed_payload_is_an_http_error_without_another_movement(self):
        event = {"id": "http-conflict", "kind": "received", "sku": "TEE-03", "quantity": 2}
        self.request("/api/events", event)
        status, body, _ = self.request("/api/events", {**event, "quantity": 9})
        self.assertEqual(status, 400)
        self.assertIn("different data", json.loads(body)["error"])

    def test_other_browser_origin_is_rejected(self):
        status, _, _ = self.request("/api/events", {}, {"Origin": "https://unrelated.invalid"})
        self.assertEqual(status, 403)

    def test_invalid_event_is_an_http_error(self):
        status, body, _ = self.request("/api/events", {})
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))

    def test_unknown_route_is_not_found(self):
        status, _, _ = self.request("/missing")
        self.assertEqual(status, 404)
