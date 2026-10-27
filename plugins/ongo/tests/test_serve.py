#!/usr/bin/env python3
"""`ongo site serve` access-log tests."""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
ONGO = PLUGIN_ROOT / "bin" / "ongo"
sys.path.insert(0, str(PLUGIN_ROOT / "lib"))

from ongo import serve  # noqa: E402


COMBINED = re.compile(
    r'^(?P<host>\S+) - - \[(?P<time>\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2} \+0000)\] '
    r'"(?P<request>(?:[^"\\]|\\.)*)" (?P<status>\d{3}) (?P<bytes>\d+|-) '
    r'"(?P<referer>(?:[^"\\]|\\.)*)" "(?P<agent>(?:[^"\\]|\\.)*)"$'
)


class AccessLogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.site = root / "site"
        self.site.mkdir()
        (self.site / "index.html").write_text("<p>hello</p>\n", encoding="utf-8")
        self.log = root / "access.log"
        self.server = serve.make_server(str(self.site), "127.0.0.1", 0, str(self.log))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temporary.cleanup()

    def request(self, method, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    def lines(self, count):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.log.exists():
                lines = self.log.read_text(encoding="utf-8").splitlines()
                if len(lines) >= count:
                    return lines
            time.sleep(0.02)
        self.fail(f"expected {count} access-log lines")

    def test_writes_combined_log_format_with_sent_bytes(self):
        status = self.request(
            "GET", "/index.html",
            {"User-Agent": "probe/1.0", "Referer": "https://example.test/"},
        )
        self.assertEqual(status, 200)
        match = COMBINED.match(self.lines(1)[0])
        self.assertIsNotNone(match)
        self.assertEqual(match["host"], "127.0.0.1")
        self.assertEqual(match["request"], "GET /index.html HTTP/1.1")
        self.assertEqual(match["status"], "200")
        self.assertEqual(match["bytes"], "13")
        self.assertEqual(match["referer"], "https://example.test/")
        self.assertEqual(match["agent"], "probe/1.0")

    def test_logs_errors_and_head_without_body_bytes(self):
        self.assertEqual(self.request("GET", "/.env"), 404)
        self.assertEqual(self.request("HEAD", "/index.html"), 200)
        missing, head = (COMBINED.match(line) for line in self.lines(2))
        self.assertEqual((missing["status"], missing["request"]), ("404", "GET /.env HTTP/1.1"))
        self.assertEqual((head["status"], head["bytes"]), ("200", "-"))
        self.assertEqual(missing["agent"], "-")

    def test_escapes_client_controlled_fields(self):
        self.request("GET", "/index.html", {"User-Agent": 'evil" 200 0 "x\x7f'})
        line = self.lines(1)[0]
        match = COMBINED.match(line)
        self.assertIsNotNone(match, line)
        self.assertEqual(match["agent"], 'evil\\" 200 0 \\"x\\x7f')

    def test_reopens_after_rotation(self):
        self.request("GET", "/index.html")
        self.lines(1)
        os.rename(self.log, self.log.with_suffix(".log.1"))
        self.request("GET", "/index.html")
        self.assertEqual(len(self.lines(1)), 1)
        self.assertEqual(len(self.log.with_suffix(".log.1").read_text().splitlines()), 1)


class AccessLogCliTests(unittest.TestCase):
    def test_unwritable_access_log_is_structured(self):
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary) / "site"
            site.mkdir()
            result = subprocess.run(
                [
                    str(ONGO), "site", "serve", "--dir", str(site), "--host", "127.0.0.1",
                    "--port", "0", "--access-log", str(Path(temporary) / "missing" / "a.log"),
                ],
                capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stderr)["error"]["code"], "access-log-unwritable")


if __name__ == "__main__":
    unittest.main()
