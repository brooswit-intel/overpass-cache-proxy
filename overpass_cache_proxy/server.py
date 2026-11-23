"""HTTP server: routes interpreter requests through the SQLite cache,
forwarding only cache misses to the configured upstream.
"""

from __future__ import annotations

import hashlib
import http.client
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

    This function always returns a 4-tuple and never raises for a network
    or transport failure — every way the send can go wrong (refused
    connection, a non-2xx status, or the peer closing mid-body) is folded
    into the return value so the caller has exactly one place to decide
    whether to count the send and whether anything is cacheable.

    Returns (status, content_type, body, complete):
    - ``status`` is 0 if no response was ever received at all (the
      connection itself failed — refused, DNS, timeout before any bytes
      came back). A real HTTP status (including non-2xx) means the
      upstream *did* receive and respond to the request.
    - ``complete`` is False whenever the response shouldn't be trusted as
      cacheable — a non-2xx upstream status, or a response (including a
      declared-200) whose body didn't match its own Content-Length because
      the connection was closed early.
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
            try:
                raw = resp.read()
            except (http.client.HTTPException, OSError):
                # The upstream accepted the request and started responding
                # (we have a status and headers) but closed before
                # delivering the body it promised. This is a genuine
                # upstream send — it must still be counted — just not one
                # whose body can be trusted or cached.
                return resp.status, content_type, b"", False
            complete = expected_length is None or len(raw) == int(expected_length)
            return resp.status, content_type, raw, complete
    except urllib.error.HTTPError as exc:
        content_type = (
            exc.headers.get("Content-Type", "text/plain") if exc.headers else "text/plain"
        )
        raw = exc.read()
        return exc.code, content_type, raw, False
    except urllib.error.URLError:
        # Never reached the upstream at all (refused/unresolvable/timed out
        # before any response). Still an attempted send from this proxy's
        # point of view, but there is no status or body to speak of.
        return 0, "text/plain", b"", False


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
            except Exception:
                # _forward is designed to never raise — every transport
                # failure it can hit is folded into its return value — but
                # this is the one place a send is counted, so an unexpected
                # exception here must still be counted as an attempted send
                # and must still produce a retryable response rather than
                # crashing the request silently.
                cache.record_upstream_send()
                self._respond_plain(502, b"upstream unreachable")
                return

            # Every branch below reached this point via a real attempt to
            # reach the upstream, whether or not it succeeded or is
            # cacheable — count it exactly once, here, unconditionally.
            cache.record_upstream_send()

            if status == 200 and complete:
                cache.put(key, CachedResponse(status=200, content_type=content_type, body=body))
                self._respond_bytes(status, content_type, body)
                return

            if status == 0:
                self._respond_plain(502, b"upstream unreachable")
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
