"""A loopback-only, credential-free fake Overpass-shaped upstream for tests.

This is the only "upstream" any test in this repo talks to. Nothing here
ever reaches the real Overpass.ch, and nothing here requires or accepts a
real API key.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeUpstream:
    def __init__(self) -> None:
        self.call_count = 0
        self.fail_next = False
        self.truncate_next = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args) -> None:  # noqa: D401
                pass

            def _data_param(self) -> str:
                if self.command == "GET":
                    parsed = urllib.parse.urlsplit(self.path)
                    query = urllib.parse.parse_qs(parsed.query)
                    return query.get("data", [""])[0]
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length else b""
                form = urllib.parse.parse_qs(raw.decode("utf-8", errors="replace"))
                return form.get("data", [""])[0]

            def _handle(self) -> None:
                data = self._data_param()
                outer.call_count += 1

                if outer.fail_next:
                    outer.fail_next = False
                    payload = b"simulated upstream failure"
                    self.send_response(500)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return

                if outer.truncate_next:
                    outer.truncate_next = False
                    full_payload = json.dumps(
                        {"echo": data, "elements": [1, 2, 3]}
                    ).encode("utf-8")
                    # Declare the real Content-Length, then only deliver a
                    # fraction of it and close the connection: a genuine
                    # mid-body truncation, not a clean short response.
                    truncated = full_payload[: max(1, len(full_payload) // 4)]
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(full_payload)))
                    self.end_headers()
                    self.wfile.write(truncated)
                    self.close_connection = True
                    return

                payload = json.dumps({"echo": data, "elements": [1, 2, 3]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                # Deliberately present as upstream-side-cached. The proxy
                # must still count this as an actual upstream send; it must
                # never infer a *local* cache hit from this header.
                self.send_header("X-Cache", "HIT")
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                self._handle()

            def do_POST(self) -> None:
                self._handle()

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> "FakeUpstream":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/interpreter"
