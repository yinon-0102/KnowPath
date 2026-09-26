"""Isolated checks for the restricted local frontend server."""

from __future__ import annotations

import http.client
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from serve_frontend import SERVICE_ID, make_handler


class RestrictedFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        cls.html = b"<!doctype html><title>KnowPath</title>"
        (root / "index.html").write_bytes(cls.html)
        (root / ".env").write_text("SECRET=must-not-be-served", encoding="utf-8")
        (root / ".learning-token.local").write_text("private-token", encoding="utf-8")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root / "index.html"))
        cls.port = cls.server.server_address[1]
        cls.server.RequestHandlerClass = make_handler(root / "index.html", cls.port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.directory.cleanup()

    def request(self, path, *, method="GET", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_only_index_is_served(self):
        for path in ("/", "/index.html", "/?unused=1"):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertEqual(body, self.html)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-KnowPath-Service"], SERVICE_ID)
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_secrets_sources_and_traversal_are_unreachable(self):
        for path in (
            "/.env", "/.learning-token.local", "/py/.env", "/py/.learning-token.local",
            "/.git/config", "/py/", "/scripts/serve_frontend.py", "/frontend/README.md",
            "/../.env", "/%2e%2e/.env", "/%2f.env", "/index.html/../.env",
            "/api/v1/health", "http://127.0.0.1:8000/api/v1/health",
        ):
            with self.subTest(path=path):
                status, _, body = self.request(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"private-token", body)
                self.assertNotIn(b"must-not-be-served", body)

    def test_external_host_is_rejected(self):
        status, _, _ = self.request("/", headers={"Host": "untrusted.example"})
        self.assertEqual(status, 403)

    def test_head_has_no_body_and_post_is_not_supported(self):
        status, headers, body = self.request("/", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(int(headers["Content-Length"]), len(self.html))
        self.assertEqual(body, b"")
        status, _, _ = self.request("/", method="POST")
        self.assertEqual(status, 501)


if __name__ == "__main__":
    unittest.main()
