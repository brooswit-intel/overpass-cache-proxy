"""Offline acceptance tests. No test here makes a live Overpass.ch call or
uses a real credential — everything talks to the loopback FakeUpstream.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
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

    def test_unsupported_path_is_rejected_not_silently_dropped(self) -> None:
        upstream = self._new_upstream()
        proxy = self._new_proxy(upstream.url)
        base = _proxy_base(proxy)
        status, _ = _get(f"{base}/not-a-real-endpoint?data=x")
        self.assertEqual(status, 501)
        self.assertEqual(upstream.call_count, 0)


if __name__ == "__main__":
    unittest.main()
