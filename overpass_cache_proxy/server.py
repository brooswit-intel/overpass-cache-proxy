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


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """MINECRAFT-566 comment 55661: urllib's default HTTPRedirectHandler
    follows a 3xx and carries every request header across to the new
    request EXCEPT Content-Length/Content-Type (verified directly against
    this interpreter's own urllib.request source, CPython 3.12) -- it does
    NOT strip Authorization-family headers, and does not restrict the new
    URL to the same host. Since ``_forward`` attaches the real X-API-Key
    credential, a malicious or merely misconfigured upstream could 3xx to a
    different host and still receive that header if the default handler
    were used.

    Returning ``None`` from ``redirect_request`` makes every ``http_error_30x``
    method return ``None`` in turn without ever opening the new URL or
    reading/forwarding anything from it (verified empirically: this makes
    ``urlopen`` raise ``HTTPError`` for the original 3xx status instead).
    That HTTPError is handled by ``_forward``'s own existing
    ``except urllib.error.HTTPError`` branch exactly like any other non-200
    upstream status -- surfaced uncached to the caller, never followed.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)

# MINECRAFT-566 comment 55657, item 4: a safe numeric Retry-After Roads (or
# any other RoadTileSource-shaped client) can actually parse -- its own
# fetchCell only ever parses the plain-integer-seconds form of this header,
# never the RFC 9110 HTTP-date form, so a non-numeric or out-of-range upstream
# value is treated the same as a missing one here, never forwarded verbatim.
_DEFAULT_RETRY_AFTER_SECONDS = 30
_MAX_RETRY_AFTER_SECONDS = 3600


def _safe_retry_after_seconds(raw_value: str | None) -> int:
    if raw_value is not None:
        try:
            parsed = int(raw_value.strip())
            if 0 <= parsed <= _MAX_RETRY_AFTER_SECONDS:
                return parsed
        except ValueError:
            pass
    return _DEFAULT_RETRY_AFTER_SECONDS


def _is_uncacheable_error_remark(body: bytes, content_type: str) -> bool:
    """MINECRAFT-566 comment 55657, item 3: Overpass can answer HTTP 200 with a
    body that is valid JSON but carries a non-empty top-level "remark" string
    -- its own error-signaling convention (``Output_JSON::write_footer``,
    the same one Roads' own ``RoadTileSource.checkForErrorRemark`` already
    treats as a failure despite the 200 status). A 200 status and a complete
    body are therefore not proof of a trustworthy answer; this must be
    checked, and must fail the request uncached, before ``cache.put`` is ever
    reached for it.
    """
    if "json" not in content_type:
        return False
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    remark = payload.get("remark")
    return isinstance(remark, str) and remark != ""


def _cache_key(method: str, path: str, data: str) -> str:
    return hashlib.sha256(f"{method}:{path}:{data}".encode("utf-8")).hexdigest()


# MINECRAFT-566 comment 55663: the documented official Overpass.ch
# interpreter contract -- the ONLY upstream_url this proxy will ever attach
# config.api_key to, mirroring Roads' own
# RoadTileSource.CREDENTIAL_BOUND_ENDPOINT/shouldSendApiKeyTo, which binds
# its own direct-mode credential to this exact same origin+path for the
# same reason: a misconfigured (or, in principle, maliciously changed)
# upstream_url must never cause the secret to be sent somewhere else.
OFFICIAL_INTERPRETER_ENDPOINT = "https://api.overpass.ch/v1/interpreter"


def _should_send_api_key_to(upstream_url: str) -> bool:
    """Checked at request time (every call), never cached from startup --
    an operator changing OVERPASS_UPSTREAM_URL mid-run (a bad env var, a
    config mistake) must not be able to silently start leaking the key to
    whatever that now points at, including on the very first request, long
    before any redirect-following question even arises.
    """
    return upstream_url == OFFICIAL_INTERPRETER_ENDPOINT


class Config:
    def __init__(
        self,
        bind_host: str = "127.0.0.1",
        bind_port: int = 8822,
        sqlite_path: str = "./overpass-cache.sqlite3",
        # MINECRAFT-566 comment 55657, item 2: the real documented Overpass.ch
        # interpreter contract, not a placeholder URL -- see also _forward's
        # X-API-Key header (not Authorization/Bearer).
        upstream_url: str = "https://api.overpass.ch/v1/interpreter",
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
                "OVERPASS_UPSTREAM_URL", "https://api.overpass.ch/v1/interpreter"
            ),
            api_key=os.environ.get("OVERPASS_API_KEY"),
        )


def _forward(config: Config, method: str, data: str):
    """Make one actual network send to the configured upstream.

    This function always returns a 5-tuple and never raises for a network
    or transport failure — every way the send can go wrong (refused
    connection, a non-2xx status, or the peer closing mid-body) is folded
    into the return value so the caller has exactly one place to decide
    whether to count the send and whether anything is cacheable.

    Returns (status, content_type, body, complete, retry_after):
    - ``status`` is 0 if no response was ever received at all (the
      connection itself failed — refused, DNS, timeout before any bytes
      came back). A real HTTP status (including non-2xx) means the
      upstream *did* receive and respond to the request.
    - ``complete`` is False whenever the response shouldn't be trusted as
      cacheable — a non-2xx upstream status, or a response (including a
      declared-200) whose body didn't match its own Content-Length because
      the connection was closed early.
    - ``retry_after`` is upstream's own raw ``Retry-After`` header value (or
      ``None`` if absent) — unvalidated; see ``_safe_retry_after_seconds``
      for turning this into something Roads can actually trust.

    MINECRAFT-566 comment 55657, item 2: the credential sent to the real
    upstream is always this proxy's OWN configured ``config.api_key``, via
    the real ``X-API-Key`` header the documented Overpass.ch contract
    actually uses — never a client-supplied credential. Keeping the secret
    proxy-side (never accepting or forwarding a caller's own credential to
    ``config.upstream_url``) is deliberate: ``upstream_url`` is operator
    configuration, not something a caller controls, so there is no redirect
    or off-host-forwarding risk from a value this function never sees.

    MINECRAFT-566 comment 55663: that is not quite enough on its own —
    ``config.api_key`` was previously attached whenever it was merely
    *set*, regardless of what ``config.upstream_url`` actually was, so a
    misconfigured upstream_url would leak the key on the INITIAL request,
    before redirect-following was ever relevant. ``_should_send_api_key_to``
    now gates this exactly like Roads' own ``shouldSendApiKeyTo`` gates its
    direct-mode credential: only the documented
    ``OFFICIAL_INTERPRETER_ENDPOINT`` ever receives it. A non-matching
    upstream_url still gets the request forwarded — this proxy's whole job
    — just without the credential (fail-open-keyless, the same choice
    Roads' own sibling function already makes, rather than refusing to
    serve a request at all over a config mistake unrelated to whether the
    request itself is sane).
    """
    headers: dict[str, str] = {}
    if config.api_key and _should_send_api_key_to(config.upstream_url):
        headers["X-API-Key"] = config.api_key

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
        with _NO_REDIRECT_OPENER.open(req, timeout=30) as resp:
            content_type = resp.headers.get("Content-Type", "application/octet-stream")
            expected_length = resp.headers.get("Content-Length")
            retry_after = resp.headers.get("Retry-After")
            try:
                raw = resp.read()
            except (http.client.HTTPException, OSError):
                # The upstream accepted the request and started responding
                # (we have a status and headers) but closed before
                # delivering the body it promised. This is a genuine
                # upstream send — it must still be counted — just not one
                # whose body can be trusted or cached.
                return resp.status, content_type, b"", False, retry_after
            complete = expected_length is None or len(raw) == int(expected_length)
            return resp.status, content_type, raw, complete, retry_after
    except urllib.error.HTTPError as exc:
        content_type = (
            exc.headers.get("Content-Type", "text/plain") if exc.headers else "text/plain"
        )
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        raw = exc.read()
        return exc.code, content_type, raw, False, retry_after
    except urllib.error.URLError:
        # Never reached the upstream at all (refused/unresolvable/timed out
        # before any response). Still an attempted send from this proxy's
        # point of view, but there is no status, body, or header to speak of.
        return 0, "text/plain", b"", False, None


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
            self._handle_interpreter("GET", parsed.path, data_values[0])

        def do_POST(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path not in SUPPORTED_PATHS:
                self._respond_plain(501, b"unsupported path")
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw_body = self.rfile.read(length) if length else b""
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()

            # MINECRAFT-566 comment 55657, item 1: Roads' own RoadTileSource
            # sends the raw Overpass QL query as the entire POST body, with
            # no form encoding and no declared Content-Type -- the real
            # Overpass.ch interpreter contract. Only a caller that actually
            # declares application/x-www-form-urlencoded is parsed as a
            # `data=`-wrapped form field; every other POST (including one
            # with no Content-Type at all) is treated as a raw body, keeping
            # existing form-data callers and Roads' own raw-body callers both
            # working through the same path.
            if content_type == "application/x-www-form-urlencoded":
                form = urllib.parse.parse_qs(raw_body.decode("utf-8", errors="replace"))
                data_values = form.get("data")
                if not data_values:
                    self._respond_plain(400, b"missing 'data' form field")
                    return
                data = data_values[0]
            else:
                data = raw_body.decode("utf-8", errors="replace")
                if not data:
                    self._respond_plain(400, b"empty request body")
                    return

            self._handle_interpreter("POST", parsed.path, data)

        def _handle_interpreter(self, method: str, path: str, data: str) -> None:
            key = _cache_key(method, path, data)
            cached = cache.get(key)
            if cached is not None:
                cache.record_local_hit()
                self._respond_bytes(cached.status, cached.content_type, cached.body)
                return

            try:
                status, content_type, body, complete, retry_after = _forward(config, method, data)
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
                if _is_uncacheable_error_remark(body, content_type):
                    # A transient runtime failure disguised as HTTP 200 --
                    # never cache it; surface it uncached so the caller
                    # (Roads' own checkForErrorRemark, or any equivalent)
                    # sees the real failure and can retry.
                    self._respond_bytes(status, content_type, body)
                    return
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

            if status in (429, 503):
                # MINECRAFT-566 comment 55657, item 4: preserve a safe numeric
                # Retry-After on every uncached 429/503 so Roads' own cooldown
                # (RoadTileSource.fetchCell's Retry-After parsing) has
                # something to honor, whether or not the real upstream
                # actually sent one.
                safe_seconds = _safe_retry_after_seconds(retry_after)
                self._respond_bytes(
                    status, content_type, body, extra_headers={"Retry-After": str(safe_seconds)}
                )
                return

            # Every other error/rate-limit response passes through uncached
            # so the caller sees the real status and can decide whether to
            # retry.
            self._respond_bytes(status, content_type, body)

        def _respond_bytes(
            self,
            status: int,
            content_type: str,
            body: bytes,
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
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
