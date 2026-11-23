# overpass-cache-proxy

A small, separately-deployable local SQLite cache proxy for the Overpass.ch
API. It sits in front of the real Overpass.ch upstream, keeps the upstream
API key server-side only, and caches successful complete query results so
repeat queries (e.g. from Earth Platform Roads, MINECRAFT-567/-566) don't
re-hit the provider.

Status: **first slice**, offline-verified only. No live Overpass.ch calls
have been made against this code; see "Provider terms" below.

## Why

- `brooswit-minecraft` consumers (starting with the Roads mod) need a local
  cache for Overpass API queries that is not baked into their own repo.
- Only one proxy process needs the real upstream API key; clients (Roads)
  never need credentials.
- Repeat identical queries should be served from local SQLite, not resent
  to the upstream, so quota usage only reflects genuinely new queries.

## Running it

```sh
python3 -m overpass_cache_proxy
```

Config is via environment variables, all optional:

| Var | Default | Meaning |
|---|---|---|
| `PROXY_BIND_HOST` | `127.0.0.1` | Bind address. Loopback by default — do not expose this process to a network beyond the host without understanding the key-handling implications below. |
| `PROXY_BIND_PORT` | `8822` | Listen port. |
| `PROXY_SQLITE_PATH` | `./overpass-cache.sqlite3` | Cache database file. A missing file starts empty; nothing pre-populates it. |
| `OVERPASS_UPSTREAM_URL` | `https://overpass.ch/api/interpreter` | Upstream interpreter endpoint. **Unconfirmed** — see "Provider terms" below. Point this at a fake/local upstream for any testing. |
| `OVERPASS_API_KEY` | unset | Upstream API key, if the provider's actual auth contract turns out to need one. Forwarded upstream only; never logged, never echoed to clients. |

## Endpoints

Both entry points share the same cache and upstream-forwarding logic. They
exist as two paths because they serve two different contracts:

- `GET/POST /api/interpreter` — the direct-provider-compatible path. Preserves
  the upstream request/response contract: `GET` with a `data` query parameter,
  or `POST` with form-encoded `data`. Any client-supplied `Authorization`
  header is passed through unchanged (for a client that already speaks
  directly to Overpass.ch and is just pointed at this proxy instead).
- `GET/POST /roads/interpreter` — the opt-in loopback mode for Roads
  (MINECRAFT-567/-566). Identical request contract, but documented as
  **requiring no credentials from the client**: the proxy's own
  `OVERPASS_API_KEY` (if configured) is used for any actual upstream send,
  so Roads never needs to hold a key. This exists as a separate path purely
  so Roads' direct-provider fallback behavior and existing saves/cache stay
  untouched if it opts out later — it does not change caching behavior.

Anything outside the supported GET-with-`data` / POST-form-with-`data`
interpreter contract is **not cached** and is documented here as an MVP
limit, not silently dropped: the proxy returns `501 Not Implemented` for a
request shape it doesn't understand, rather than guessing. This is a
deliberate first-slice scope boundary, not an attempt to describe fuller
Overpass QL/XML support.

## Caching rules

- Only a **complete, successful (HTTP 200)** upstream response is cached.
  Rate limit responses, errors, and partial/truncated reads are never
  cached and remain retryable on the next request.
- A response carrying `X-Cache: HIT` from the *upstream* is still an actual
  network send from this proxy's point of view, and is counted as an
  upstream send, not a local hit — see "Metrics" below. The proxy does not
  infer a local cache hit from any upstream response header.
- Cache key is derived from the normalized request (method, path, and the
  query/form `data` value) — not from timing, headers, or anything else.

## Metrics

`GET /_internal/stats` returns:

```json
{"local_hits": 0, "upstream_sends": 0}
```

Both counters are cumulative since the SQLite file was created (i.e. they
survive a process restart, unlike pure in-memory counters) and distinguish:

- `local_hits` — a request whose response was served entirely from SQLite,
  with no network send to the configured upstream.
- `upstream_sends` — a request that actually went out over the network to
  the configured upstream, regardless of what that upstream's own response
  said about its own cache state.

Taking a before/after delta of this endpoint around a query is the
supported way to assert "1 local hit, 0 upstream sends" (the MINECRAFT-567
integration contract) without needing to infer it from logs.

## Provider terms (unconfirmed)

The exact Overpass.ch request/response contract (endpoint path, whether an
API key is required and how it's presented, rate-limit response shape) has
**not** been confirmed against the live provider as part of this first
slice — no live calls were made. `OVERPASS_UPSTREAM_URL` defaults to a
plausible endpoint shape but this is a placeholder, not a confirmed
integration. Treat enabling real upstream traffic (setting a live
`OVERPASS_UPSTREAM_URL`/`OVERPASS_API_KEY` and actually deploying this) as
a separate, explicitly-authorized follow-up, not something this slice
claims to have done. All current verification in this repo runs against a
local fake upstream (`tests/fake_upstream.py`) over loopback only.

## Security notes

- Binds to loopback by default. There is no authentication in front of this
  proxy itself — anything that can reach `PROXY_BIND_HOST:PROXY_BIND_PORT`
  can use it. Don't bind it to a non-loopback address without adding that.
- The upstream API key, if set, is never written to logs or returned in any
  response body/header to a client.

## Testing

```sh
python3 -m unittest discover -s tests -v
```

Tests spin up a local fake Overpass-shaped upstream on loopback (no network
access, no credentials, no live provider) and exercise:

- start-empty (fresh SQLite file has zero cached entries),
- hit (repeat identical query served from cache, zero upstream sends),
- restart/no-network (killing and restarting the proxy process against the
  same SQLite file still serves the warm entry even with the fake upstream
  taken offline),
- retry (a failing upstream response is never cached and is retried on the
  next request).
