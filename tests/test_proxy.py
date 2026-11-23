"""Offline acceptance tests. No test here makes a live Overpass.ch call or
uses a real credential — everything talks to the loopback FakeUpstream.
"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from overpass_cache_proxy.server import Config, ProxyServer
from overpass_cache_proxy.cache import Cache

from fake_upstream import FakeUpstream


def _start_proxy(upstream_url: str, sqlite_path: str) -> ProxyServer:
    config = Config(
        bind_host="127.0.0.1",
        bind_port=0,
        sqlite_path=sqlite_path,
        upstream_url=upstream_url,
    )
    server = ProxyServer(config, Cache(sqlite_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _proxy_base(server: ProxyServer) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _get_with_headers(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}


def _post_raw(
    url: str,
    body: bytes,
    content_type: str | None,
    extra_request_headers: dict | None = None,
) -> tuple[int, bytes]:
    """POST exactly the given raw bytes, with no form encoding -- this is the
    real wire shape Roads' own RoadTileSource.fetchCell sends (no form
    wrapping, and ordinarily no Content-Type header at all).

    Uses http.client directly, not urllib.request: urllib's own
    AbstractHTTPHandler silently injects
    ``Content-type: application/x-www-form-urlencoded`` whenever a POST has
    a body and no Content-Type header is set, which would defeat the exact
    no-Content-Type wire shape this helper exists to reproduce.
    """
    parsed = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        headers = dict(extra_request_headers or {})
        if content_type is not None:
            headers["Content-Type"] = content_type
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _post_form(url: str, form: dict) -> tuple[int, bytes]:
    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _stats(base: str) -> dict:
    status, body = _get(f"{base}/_internal/stats")
    assert status == 200, body
    return json.loads(body)


class ProxyAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.sqlite_path = os.path.join(self._tmpdir.name, "cache.sqlite3")
        self._servers: list[ProxyServer] = []
        self._upstreams: list[FakeUpstream] = []

    def tearDown(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        for upstream in self._upstreams:
            upstream.stop()
        self._tmpdir.cleanup()

    def _new_upstream(self) -> FakeUpstream:
        upstream = FakeUpstream().start()
        self._upstreams.append(upstream)
        return upstream

    def _new_proxy(self, upstream_url: str, sqlite_path: str | None = None) -> ProxyServer:
        server = _start_proxy(upstream_url, sqlite_path or self.sqlite_path)
        self._servers.append(server)
        return server

    def test_start_empty(self) -> None:
        upstream = self._new_upstream()
        proxy = self._new_proxy(upstream.url)
        stats = _stats(_proxy_base(proxy))
        self.assertEqual(stats, {"local_hits": 0, "upstream_sends": 0})
        self.assertEqual(upstream.call_count, 0)

    def test_hit_contract_one_local_hit_zero_upstream_sends(self) -> None:
        """The MINECRAFT-567 integration contract: with a warm proxy, one
        repeat identical query produces 1 local hit and 0 upstream sends.
        """
        upstream = self._new_upstream()
        proxy = self._new_proxy(upstream.url)
        base = _proxy_base(proxy)
        query_url = f"{base}/roads/interpreter?data=deterministic-query-1"

        before = _stats(base)
        status1, body1 = _get(query_url)
        after_first = _stats(base)

        self.assertEqual(status1, 200)
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(after_first["upstream_sends"] - before["upstream_sends"], 1)
        self.assertEqual(after_first["local_hits"] - before["local_hits"], 0)

        status2, body2 = _get(query_url)
        after_second = _stats(base)

        self.assertEqual(status2, 200)
        self.assertEqual(body1, body2)
        # The fake upstream was not contacted again...
        self.assertEqual(upstream.call_count, 1)
        # ...and the proxy's own counters agree: exactly 1 hit, 0 sends,
        # on top of what the first request already recorded.
        self.assertEqual(after_second["local_hits"] - after_first["local_hits"], 1)
        self.assertEqual(after_second["upstream_sends"] - after_first["upstream_sends"], 0)

    def test_upstream_x_cache_hit_still_counts_as_upstream_send(self) -> None:
        upstream = self._new_upstream()
        proxy = self._new_proxy(upstream.url)
        base = _proxy_base(proxy)

        before = _stats(base)
        status, _ = _get(f"{base}/api/interpreter?data=x-cache-check")
        after = _stats(base)

        self.assertEqual(status, 200)
        # FakeUpstream always answers with X-Cache: HIT on success; the
        # proxy must still count this first, genuinely-new query as an
        # upstream send, not infer a local hit from that header.
        self.assertEqual(after["upstream_sends"] - before["upstream_sends"], 1)
        self.assertEqual(after["local_hits"] - before["local_hits"], 0)

    def test_restart_and_no_network_still_serves_warm_entry(self) -> None:
        upstream = self._new_upstream()
        proxy1 = self._new_proxy(upstream.url)
        base1 = _proxy_base(proxy1)
        query_url_path = "/roads/interpreter?data=restart-query"

        status, warm_body = _get(f"{base1}{query_url_path}")
        self.assertEqual(status, 200)
        self.assertEqual(upstream.call_count, 1)

        proxy1.shutdown()
        proxy1.server_close()
        upstream.stop()  # simulate no network / upstream gone

        # New process-equivalent: fresh ProxyServer, same sqlite file, and
        # an upstream URL that nothing is listening on.
        proxy2 = self._new_proxy("http://127.0.0.1:1/api/interpreter")
        base2 = _proxy_base(proxy2)

        before = _stats(base2)
        status2, body2 = _get(f"{base2}{query_url_path}")
        after = _stats(base2)

        self.assertEqual(status2, 200)
        self.assertEqual(body2, warm_body)
        self.assertEqual(after["local_hits"] - before["local_hits"], 1)
        self.assertEqual(after["upstream_sends"] - before["upstream_sends"], 0)

    def test_failed_upstream_response_is_never_cached_and_is_retried(self) -> None:
        upstream = self._new_upstream()
        proxy = self._new_proxy(upstream.url)
        base = _proxy_base(proxy)
        query_url = f"{base}/roads/interpreter?data=retry-query"

        upstream.fail_next = True
        before = _stats(base)
        status1, body1 = _get(query_url)
        after_failure = _stats(base)

        self.assertEqual(status1, 500)
        self.assertEqual(body1, b"simulated upstream failure")
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(after_failure["upstream_sends"] - before["upstream_sends"], 1)
        self.assertEqual(after_failure["local_hits"] - before["local_hits"], 0)

        # Retry: the failure must not have been cached, so this is a
        # genuinely new upstream send, not a local hit.
        status2, body2 = _get(query_url)
        after_retry = _stats(base)

        self.assertEqual(status2, 200)
        self.assertEqual(upstream.call_count, 2)
        self.assertEqual(after_retry["upstream_sends"] - after_failure["upstream_sends"], 1)
        self.assertEqual(after_retry["local_hits"] - after_failure["local_hits"], 0)

        # And now it's warm: a third identical request is a local hit.
        status3, body3 = _get(query_url)
        after_hit = _stats(base)

        self.assertEqual(status3, 200)
        self.assertEqual(body3, body2)
        self.assertEqual(upstream.call_count, 2)
        self.assertEqual(after_hit["local_hits"] - after_retry["local_hits"], 1)
        self.assertEqual(after_hit["upstream_sends"] - after_retry["upstream_sends"], 0)

    def test_truncated_response_is_retryable_not_cached_and_counted_as_a_send(self) -> None:
        """Regression for the CHANGES_REQUESTED finding: a peer that
        declares a Content-Length and then closes mid-body must not crash
        the request, must not be cached, and must still be counted as one
        genuine upstream send (the upstream did receive the request).
        """
        upstream = self._new_upstream()
        proxy = self._new_proxy(upstream.url)
        base = _proxy_base(proxy)
        query_url = f"{base}/roads/interpreter?data=truncated-query"

        upstream.truncate_next = True
        before = _stats(base)
        status1, body1 = _get(query_url)
        after_truncated = _stats(base)

        self.assertEqual(status1, 502)
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(after_truncated["upstream_sends"] - before["upstream_sends"], 1)
        self.assertEqual(after_truncated["local_hits"] - before["local_hits"], 0)

        # Not cached: a retry genuinely reaches the upstream again.
        status2, body2 = _get(query_url)
        after_retry = _stats(base)

        self.assertEqual(status2, 200)
        self.assertEqual(upstream.call_count, 2)
        self.assertEqual(after_retry["upstream_sends"] - after_truncated["upstream_sends"], 1)
        self.assertEqual(after_retry["local_hits"] - after_truncated["local_hits"], 0)

        # And now it's warm.
        status3, body3 = _get(query_url)
        after_hit = _stats(base)

        self.assertEqual(status3, 200)
        self.assertEqual(body3, body2)
        self.assertEqual(upstream.call_count, 2)
        self.assertEqual(after_hit["local_hits"] - after_retry["local_hits"], 1)
        self.assertEqual(after_hit["upstream_sends"] - after_retry["upstream_sends"], 0)

    def test_unsupported_path_is_rejected_not_silently_dropped(self) -> None:
        upstream = self._new_upstream()
        proxy = self._new_proxy(upstream.url)
        base = _proxy_base(proxy)
        status, _ = _get(f"{base}/not-a-real-endpoint?data=x")
        self.assertEqual(status, 501)
        self.assertEqual(upstream.call_count, 0)

    def test_roads_raw_post_body_is_accepted_and_cached_like_any_other_query(self) -> None:
        """MINECRAFT-566 comment 55657, item 1: Roads' real RoadTileSource
        POSTs the Overpass QL query as the entire raw body, with no form
        encoding and no Content-Type header at all -- not a `data=`-wrapped
        form field. This reproduces that exact wire shape (no Content-Type
        header set) rather than a Roads-side fake.
        """
        upstream = self._new_upstream()
        proxy = self._new_proxy(upstream.url)
        base = _proxy_base(proxy)
        query = '[out:json][timeout:30];(way["highway"~"^(primary)$"](41.0,-96.0,41.1,-95.9););out geom;'

        before = _stats(base)
        status1, body1 = _post_raw(f"{base}/roads/interpreter", query.encode("utf-8"), content_type=None)
        after_first = _stats(base)

        self.assertEqual(status1, 200, body1)
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(after_first["upstream_sends"] - before["upstream_sends"], 1)

        # Identical raw-body request again: must be a cache hit, not a
        # second upstream send.
        status2, body2 = _post_raw(f"{base}/roads/interpreter", query.encode("utf-8"), content_type=None)
        after_second = _stats(base)

        self.assertEqual(status2, 200)
        self.assertEqual(body1, body2)
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(after_second["local_hits"] - after_first["local_hits"], 1)
        self.assertEqual(after_second["upstream_sends"] - after_first["upstream_sends"], 0)

    def test_existing_form_encoded_post_still_works_alongside_raw_post(self) -> None:
        """Preserving existing form-data handling for a caller that actually
        declares application/x-www-form-urlencoded, in the same proxy
        instance a raw-body Roads-shaped caller also uses.
        """
        upstream = self._new_upstream()
        proxy = self._new_proxy(upstream.url)
        base = _proxy_base(proxy)

        status, body = _post_form(f"{base}/api/interpreter", {"data": "form-encoded-query"})
        self.assertEqual(status, 200, body)
        self.assertEqual(upstream.call_count, 1)

    def test_malformed_post_with_no_body_and_no_form_field_is_rejected(self) -> None:
        upstream = self._new_upstream()
        proxy = self._new_proxy(upstream.url)
        base = _proxy_base(proxy)

        status, _ = _post_raw(f"{base}/roads/interpreter", b"", content_type=None)
        self.assertEqual(status, 400)
        self.assertEqual(upstream.call_count, 0)

    def test_proxy_talks_the_real_overpass_ch_contract_to_upstream(self) -> None:
        """MINECRAFT-566 comment 55657, item 2: the proxy's own outbound call
        must use the real documented contract -- api.overpass.ch/v1 shape and
        an X-API-Key header carrying the PROXY's own configured secret, never
        a client-supplied credential.
        """
        upstream = self._new_upstream()
        config = Config(
            bind_host="127.0.0.1",
            bind_port=0,
            sqlite_path=self.sqlite_path,
            upstream_url=upstream.url,
            api_key="proxy-held-secret",
        )
        server = ProxyServer(config, Cache(self.sqlite_path))
        self._servers.append(server)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = _proxy_base(server)

        # A client-supplied Authorization header must never reach upstream --
        # only the proxy's own configured api_key, via X-API-Key, does.
        status, body = _post_raw(
            f"{base}/roads/interpreter",
            b"a raw overpass ql query",
            content_type=None,
            extra_request_headers={"Authorization": "Bearer a-client-supplied-credential"},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(upstream.last_seen_api_key, "proxy-held-secret")

    def test_default_upstream_url_is_the_real_documented_contract(self) -> None:
        self.assertEqual(Config().upstream_url, "https://api.overpass.ch/v1/interpreter")

    def test_200_with_error_remark_is_never_cached_then_recovers_and_caches(self) -> None:
        """MINECRAFT-566 comment 55657, item 3: an HTTP 200 whose JSON body
        carries a non-empty top-level "remark" (Overpass's own runtime-error
        signal) must never be cached. A subsequent identical request, once
        upstream actually succeeds, must genuinely re-reach upstream (not a
        stale cached error) and then cache normally.
        """
        upstream = self._new_upstream()
        proxy = self._new_proxy(upstream.url)
        base = _proxy_base(proxy)
        query_url = f"{base}/roads/interpreter?data=transient-error-query"

        upstream.remark_next = True
        before = _stats(base)
        status1, body1 = _get(query_url)
        after_remark = _stats(base)

        self.assertEqual(status1, 200)
        self.assertEqual(json.loads(body1).get("remark"), "runtime error: Query timed out.")
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(after_remark["upstream_sends"] - before["upstream_sends"], 1)
        self.assertEqual(after_remark["local_hits"] - before["local_hits"], 0)

        # Not cached: a second identical request genuinely re-reaches
        # upstream (now a real, recoverable success).
        status2, body2 = _get(query_url)
        after_recovery = _stats(base)

        self.assertEqual(status2, 200)
        self.assertNotIn("remark", json.loads(body2))
        self.assertEqual(upstream.call_count, 2)
        self.assertEqual(after_recovery["upstream_sends"] - after_remark["upstream_sends"], 1)
        self.assertEqual(after_recovery["local_hits"] - after_remark["local_hits"], 0)

        # And now it's genuinely warm.
        status3, body3 = _get(query_url)
        after_hit = _stats(base)

        self.assertEqual(status3, 200)
        self.assertEqual(body3, body2)
        self.assertEqual(upstream.call_count, 2)
        self.assertEqual(after_hit["local_hits"] - after_recovery["local_hits"], 1)
        self.assertEqual(after_hit["upstream_sends"] - after_recovery["upstream_sends"], 0)

    def test_uncached_429_preserves_a_safe_numeric_retry_after(self) -> None:
        self._assert_safe_retry_after_preserved(status=429, upstream_retry_after="17")

    def test_uncached_503_preserves_a_safe_numeric_retry_after(self) -> None:
        self._assert_safe_retry_after_preserved(status=503, upstream_retry_after="42")

    def test_uncached_429_without_an_upstream_retry_after_still_gets_a_safe_default(self) -> None:
        self._assert_safe_retry_after_preserved(status=429, upstream_retry_after=None)

    def test_non_numeric_upstream_retry_after_falls_back_to_a_safe_default(self) -> None:
        # RFC 9110 also permits an HTTP-date Retry-After; Roads' own
        # RoadTileSource only parses the numeric-seconds form, so the proxy
        # must not forward a non-numeric value verbatim.
        self._assert_safe_retry_after_preserved(
            status=503, upstream_retry_after="Wed, 21 Oct 2026 07:28:00 GMT", expect_default=True
        )

    def _assert_safe_retry_after_preserved(
        self, status: int, upstream_retry_after: str | None, expect_default: bool = False
    ) -> None:
        upstream = self._new_upstream()
        proxy = self._new_proxy(upstream.url)
        base = _proxy_base(proxy)

        upstream.rate_limit_next = status
        upstream.rate_limit_retry_after_next = upstream_retry_after
        before = _stats(base)
        resp_status, headers = _get_with_headers(f"{base}/roads/interpreter?data=rate-limited-query")
        after = _stats(base)

        self.assertEqual(resp_status, status)
        self.assertEqual(after["upstream_sends"] - before["upstream_sends"], 1)
        self.assertEqual(after["local_hits"] - before["local_hits"], 0)
        retry_after_header = headers.get("Retry-After")
        self.assertIsNotNone(retry_after_header, "Expected a Retry-After header on an uncached 429/503")
        parsed = int(retry_after_header)
        self.assertGreaterEqual(parsed, 0)
        if expect_default or upstream_retry_after is None:
            from overpass_cache_proxy.server import _DEFAULT_RETRY_AFTER_SECONDS

            self.assertEqual(parsed, _DEFAULT_RETRY_AFTER_SECONDS)
        else:
            self.assertEqual(parsed, int(upstream_retry_after))

        # Never cached: a retry genuinely reaches upstream again.
        status_again, _ = _get(f"{base}/roads/interpreter?data=rate-limited-query")
        after_retry = _stats(base)
        self.assertEqual(status_again, 200)
        self.assertEqual(after_retry["upstream_sends"] - after["upstream_sends"], 1)
        self.assertEqual(after_retry["local_hits"] - after["local_hits"], 0)

    def test_offline_pair_acceptance_cold_then_fresh_roads_cache_warm_proxy_cache(self) -> None:
        """MINECRAFT-566 comment 55657, item 5: the exact offline pair
        acceptance contract against a REAL running proxy and a fake
        upstream, driven with a request byte-for-byte matching what Roads'
        own RoadTileSource.fetchCell actually sends (raw POST body, no form
        encoding, no Content-Type header, the real /roads/interpreter path).

        Honest scope note: this repo's test harness cannot start a real
        Roads JVM, so "Roads HTTP send" here is this test's own POST call to
        the real, live proxy using Roads' exact wire format -- not a second,
        separate fake object asserting on something else. The proxy side
        (local_hits/upstream_sends) is the real Cache/Config/ProxyServer
        code, not a stand-in.
        """
        upstream = self._new_upstream()
        proxy = self._new_proxy(upstream.url)
        base = _proxy_base(proxy)
        query = '[out:json][timeout:30];(way["highway"~"^(secondary)$"](41.0,-96.0,41.1,-95.9););out geom;'
        url = f"{base}/roads/interpreter"

        # Cold: a fresh Roads-shaped request must reach the real fake
        # upstream exactly once.
        before = _stats(base)
        roads_http_sends = 0
        status1, body1 = _post_raw(url, query.encode("utf-8"), content_type=None)
        roads_http_sends += 1
        after_cold = _stats(base)

        self.assertEqual(status1, 200, body1)
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(after_cold["upstream_sends"] - before["upstream_sends"], 1)
        self.assertEqual(after_cold["local_hits"] - before["local_hits"], 0)

        # "Fresh Roads cache" (a real Roads process would not yet have this
        # exact query cached either, since it's never seen it before) but a
        # now-warm proxy SQLite cache: the identical method/path/query must
        # yield +1 Roads HTTP send (this POST), +1 proxy-side SQLite hit,
        # +0 further upstream sends.
        status2, body2 = _post_raw(url, query.encode("utf-8"), content_type=None)
        roads_http_sends += 1
        after_warm = _stats(base)

        self.assertEqual(status2, 200, body2)
        self.assertEqual(body1, body2)
        self.assertEqual(upstream.call_count, 1, "Expected zero further upstream sends against a warm proxy cache")
        self.assertEqual(after_warm["local_hits"] - after_cold["local_hits"], 1)
        self.assertEqual(after_warm["upstream_sends"] - after_cold["upstream_sends"], 0)
        self.assertEqual(roads_http_sends, 2, "One Roads-shaped HTTP send for the cold request, one for the warm one")


if __name__ == "__main__":
    unittest.main()
