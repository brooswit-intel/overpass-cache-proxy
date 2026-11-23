"""HTTP server: routes interpreter requests through the SQLite cache,
forwarding only cache misses to the configured upstream.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .cache import Cache, CachedResponse

SUPPORTED_PATHS = {"/api/interpreter", "/roads/interpreter"}


def _cache_key(method: str, path: str, data: str) -> str:
    return hashlib.sha256(f"{method}:{path}:{data}".encode("utf-8")).hexdigest()


class Config:
    def __init__(
        self,
        bind_host: str = "127.0.0.1",
        bind_port: int = 8822,
        sqlite_path: str = "./overpass-cache.sqlite3",
        upstream_url: str = "https://overpass.ch/api/interpreter",
        api_key: str | None = None,
    ) -> None:
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.sqlite_path = sqlite_path
        self.upstream_url = upstream_url
        self.api_key = api_key

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            bind_host=os.environ.get("PROXY_BIND_HOST", "127.0.0.1"),
            bind_port=int(os.environ.get("PROXY_BIND_PORT", "8822")),
            sqlite_path=os.environ.get("PROXY_SQLITE_PATH", "./overpass-cache.sqlite3"),
            upstream_url=os.environ.get(
                "OVERPASS_UPSTREAM_URL", "https://overpass.ch/api/interpreter"
            ),
            api_key=os.environ.get("OVERPASS_API_KEY"),
        )


def _forward(config: Config, method: str, data: str, client_auth: str | None):
    """Make one actual network send to the configured upstream.

    Returns (status, content_type, body, complete). ``complete`` is False
    whenever the response shouldn't be trusted as cacheable — a non-2xx
    upstream status, or a 200 whose body didn't match its own
    Content-Length.
    """
    headers: dict[str, str] = {}
    if client_auth:
        headers["Authorization"] = client_auth
    elif config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    if method == "GET":
        url = f"{config.upstream_url}?{urllib.parse.urlencode({'data': data})}"
        req = urllib.request.Request(url, headers=headers, method="GET")
    else:
        body = urllib.parse.urlencode({"data": data}).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(
            config.upstream_url, data=body, headers=headers, method="POST"
        )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_type = resp.headers.get("Content-Type", "application/octet-stream")
            expected_length = resp.headers.get("Content-Length")
            raw = resp.read()
            complete = expected_length is None or len(raw) == int(expected_length)
            return resp.status, content_type, raw, complete
    except urllib.error.HTTPError as exc:
        content_type = (
            exc.headers.get("Content-Type", "text/plain") if exc.headers else "text/plain"
        )
        raw = exc.read()
        return exc.code, content_type, raw, False


def make_handler(cache: Cache, config: Config):
    class Handler(BaseHTTPRequestHandler):
        server_version = "overpass-cache-proxy/0.1"

        def log_message(self, fmt, *args):  # noqa: D401 - silence default stderr logging
            # Deliberately silent: the default BaseHTTPRequestHandler logger
            # would print the raw request line, which can carry the query
            # body. Never log coordinates/query/headers/keys.
            pass

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/_internal/stats":
                self._respond_json(200, cache.stats())
                return
            if parsed.path not in SUPPORTED_PATHS:
                self._respond_plain(501, b"unsupported path")
                return
            query = urllib.parse.parse_qs(parsed.query)
            data_values = query.get("data")
            if not data_values:
                self._respond_plain(400, b"missing 'data' query parameter")
                return
            self._handle_interpreter(
                "GET", parsed.path, data_values[0], self.headers.get("Authorization")
            )

        def do_POST(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path not in SUPPORTED_PATHS:
                self._respond_plain(501, b"unsupported path")
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw_body = self.rfile.read(length) if length else b""
            form = urllib.parse.parse_qs(raw_body.decode("utf-8", errors="replace"))
            data_values = form.get("data")
            if not data_values:
                self._respond_plain(400, b"missing 'data' form field")
                return
            self._handle_interpreter(
                "POST", parsed.path, data_values[0], self.headers.get("Authorization")
            )

        def _handle_interpreter(
            self, method: str, path: str, data: str, client_auth: str | None
        ) -> None:
            key = _cache_key(method, path, data)
            cached = cache.get(key)
            if cached is not None:
                cache.record_local_hit()
                self._respond_bytes(cached.status, cached.content_type, cached.body)
                return

            # Only the direct-provider-compatible path passes a client's own
            # Authorization through; the Roads loopback path never requires
            # (or forwards) client credentials.
            forwarded_auth = client_auth if path.startswith("/api/") else None

            try:
                status, content_type, body, complete = _forward(
                    config, method, data, forwarded_auth
                )
            except urllib.error.URLError:
                cache.record_upstream_send()
                self._respond_plain(502, b"upstream unreachable")
                return

            # Every branch below reached an actual upstream send, whether or
            # not the response is cacheable.
            cache.record_upstream_send()

            if status == 200 and complete:
                cache.put(key, CachedResponse(status=200, content_type=content_type, body=body))
                self._respond_bytes(status, content_type, body)
                return

            if status == 200 and not complete:
                # A truncated "successful" response is never cached, and is
                # not forwarded as-if-good either — surface it as retryable.
                self._respond_plain(502, b"incomplete upstream response")
                return

            # Error/rate-limit responses pass through uncached so the caller
            # sees the real status and can decide whether to retry.
            self._respond_bytes(status, content_type, body)

        def _respond_bytes(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _respond_plain(self, status: int, body: bytes) -> None:
            self._respond_bytes(status, "text/plain; charset=utf-8", body)

        def _respond_json(self, status: int, obj) -> None:
            self._respond_bytes(status, "application/json", json.dumps(obj).encode("utf-8"))

    return Handler


class ProxyServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer that also owns (and closes) the cache's sqlite
    connection, so callers have one object to shut down cleanly.
    """

    daemon_threads = True

    def __init__(self, config: Config, cache: Cache) -> None:
        super().__init__((config.bind_host, config.bind_port), make_handler(cache, config))
        self.proxy_cache = cache

    def server_close(self) -> None:
        super().server_close()
        self.proxy_cache.close()


def run(config: Config | None = None) -> ProxyServer:
    config = config or Config.from_env()
    cache = Cache(config.sqlite_path)
    return ProxyServer(config, cache)


def main() -> None:
    server = run()
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
