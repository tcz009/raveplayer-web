# Resolver service (`/api/resolve`)

**Branch:** `feat/resolver-service` · **Base:** `d0f0621` · **Scope:** new files only
(`core/resolver/{__init__,base,direct,podomatic,cache,service}.py`,
`test/test_resolver_service.py`, this document). `app.py`, templates and static
assets are intentionally untouched; the parent agent registers the blueprint.

The resolver turns a provider/page URL into a **direct, playable media URL**
plus the request headers a player should send. It does **not** proxy media
bytes and never answers with an HTTP redirect. The response is JSON that the
web player or the Kodi add-on can consume.

Two adapters ship here:

| Adapter | Owns | Result |
|---|---|---|
| `podomatic` | `*.podomatic.com` enclosure URLs and `pdcn.co/e/...` links | canonical `https://<show>.podomatic.com/enclosure/...` |
| `direct` | allowlisted public `http(s)` `.mp3` / `.m4a` URLs | the same URL, normalised, plus optional headers |

Provider adapters for Mediafire, Google Drive and Mega are **interfaces only**
in this branch; see [Plugging in new adapters](#plugging-in-new-adapters).

---

## API contract

### Request

```http
GET /api/resolve?u=https%3A%2F%2Fpdcn.co%2Fe%2Fshow.podomatic.com%2Fenclosure%2Fx.mp3
```

or:

```http
POST /api/resolve
Content-Type: application/json

{"url": "https://pdcn.co/e/show.podomatic.com/enclosure/x.mp3"}
```

* `u` and `url` are accepted as aliases (`GET` query or JSON/form field).
* Maximum URL length is **2048 bytes**; request bodies over **8 KiB** are
  rejected before parsing.
* Only `http` and `https` are accepted. Credentials in the URL, backslashes,
  control characters and fragments are rejected or stripped (fragments are
  stripped).
* The endpoint accepts JSON only for the normal path; a form-encoded `url=`
  field is also accepted for simple clients.

### Success — `200`

```json
{
  "ok": true,
  "url": "https://dejavufmpodcast.podomatic.com/enclosure/2026-11-07T09_15_50-08_00.mp3?_=1794071789.17925363",
  "headers": {
    "User-Agent": "RavePlayer/3.0 (resolver; +https://github.com/tcz009/plugin.audio.raveplayer)",
    "Accept": "audio/mpeg, audio/mp4;q=0.9, audio/*;q=0.8, */*;q=0.1",
    "Referer": "https://www.podomatic.com/"
  },
  "expires": 1762685000,
  "source": "podomatic",
  "host": "dejavufmpodcast.podomatic.com"
}
```

| Field | Meaning |
|---|---|
| `url` | Direct media URL; already canonical, https where the provider supports it |
| `headers` | Request headers the player should send (may be `{}`) |
| `expires` | Unix epoch seconds after which the URL may stop working; clients should re-resolve after this |
| `source` | Adapter id (`podomatic`, `direct`, or a provider adapter id) |
| `host` | Host of the resolved URL (for telemetry/UI) |

Every response is `Cache-Control: no-store` and
`X-Content-Type-Options: nosniff`. There is **no** `Location` header and no
3xx path in this service.

### Errors

```json
{"ok": false, "error": "no resolver is configured for this URL", "code": "unknown"}
```

| HTTP | `code` | When |
|---|---|---|
| `400` | `invalid` | Missing/oversized/malformed request URL, bad JSON body |
| `400` | `unknown` | No adapter owns the URL (includes arbitrary/non-allowlisted hosts) |
| `400` | `blocked` | Private/loopback/reserved destination, credentials, host allowlist refusal, or DNS that resolves private |
| `400` | `unsupported` | Known provider but a page/playlist/non-audio URL (e.g. a Podomatic episode page) |
| `401` | `unauthorized` | No valid web session (see [Auth](#auth)) |
| `404` | `not_found` | Provider says the media is gone (or optional HEAD probe got 404/410) |
| `429` | `rate_limited` | Per-host token bucket empty; includes `retry_after` in JSON and a `Retry-After` header |
| `502` | `upstream` | Provider failed, returned an unsafe result, or an unexpected adapter error |
| `504` | `timeout` | Provider exceeded the bounded timeout |

`code` values are stable; `error` is a human-readable sentence and never
contains the submitted URL or a signed query string. Unknown error types are
mapped to `502 upstream` rather than leaking a stack trace.

### Auth

In the full web app, `app.py`'s global `before_request` guard already protects
every `/api/` path. The parent only needs:

```python
from core.resolver.service import resolver_bp

app.register_blueprint(resolver_bp)
```

Requests without a valid session are rejected by that existing guard, exactly
like `/api/menu` and `/api/downloads` (JSON `401
{"error": "authentication required"}`). The blueprint repeats a session check
as defence in depth; when mounted standalone (tests/embeds) it returns `401
{"ok": false, "error": "authentication required", "code": "unauthorized"}`.
`RESOLVER_AUTH_CHECK` (Flask config, callable or bool) can inject a checker for
an embedding application; it is *not* an auth bypass and defaults to the app's
`_session_valid`.

There is no API-token mode. A Kodi client must present the same authenticated
session cookie as the web UI (or call through a component that already has
one). Adding a separate service credential is deliberately out of scope for
this branch.

---

## Security model (SSRF / open proxy)

The endpoint is intentionally **not** a generic proxy.

1. **Adapter ownership.** A URL is resolved only if a known adapter matches
   it. The `direct` adapter requires a `.mp3`/`.m4a` path on a host in its
   allowlist; everything else is `400 unknown`.
2. **Allowlist.** Matching is exact-host or dot-boundary subdomain
   (`example.com` matches `cdn.example.com`, never `notexample.com`).
   Wildcards are rejected. `RESOLVER_DIRECT_HOSTS` replaces the curated list;
   set it empty to disable direct URLs entirely.
3. **Private/loopback rejection.** `127.0.0.0/8`, `10/8`, `172.16/12`,
   `192.168/16`, `169.254/16`, `::1`, ULA/link-local, multicast, reserved,
   `localhost`, single-label Docker/K8s names and `.local`/`.internal` names
   are rejected. DNS is resolved and **every** answer must be public;
   mixed public/private answers fail closed. Failures/DNS errors are `400
   blocked`. This guard deliberately ignores `RAVEWEB_ALLOW_PRIVATE=1`:
   unlike page scraping, this service returns URLs to clients elsewhere.
4. **No redirects.** Adapters return a URL; they never ask a client to follow
   a redirect. The optional HEAD probe uses a redirect-free HTTP client and
   reports 3xx/4xx/5xx as errors rather than following them.
5. **No media proxying.** The service never opens the media stream, so it
   cannot be used to exfiltrate bytes or bypass client-side egress rules.
6. **Header hygiene.** Only safe request headers are returned. `Cookie`,
   `Set-Cookie`, `Authorization`, `Host`, `Content-Length`,
   `Transfer-Encoding`, proxy/forwarding headers and CRLF injection are
   rejected; an adapter returning them produces `502 upstream`.
7. **Result re-check.** The service independently validates the adapter's
   returned URL (scheme/length/credentials) and re-runs the public-host check.
   A buggy provider adapter cannot return a private URL.

Rate limiting and caching are per target host:

* **Token bucket** (`RESOLVER_RATE_PER_MINUTE`, `RESOLVER_BURST`) is consumed
  only on a cache miss, just before the adapter runs. Different hosts have
  independent buckets; a cached resolution does not consume a token.
* **Positive cache** TTL `RESOLVER_CACHE_TTL` (and never beyond the adapter's
  `expires`), **negative cache** for definitive `404`/`upstream` failures for
  `RESOLVER_NEGATIVE_TTL`. Timeouts, rate limits, blocked and unsupported
  results are **not** negatively cached.
* **Single-flight per host.** Concurrent requests for the same URL share the
  first result; different URLs on the same host are serialised so a provider
  never sees parallel resolution work from one worker. This is per process,
  like the provider fetch gate; multiple gunicorn workers each have a cache.

---

## Configuration

All values are read when the blueprint builds its default service (first
request) or when `build_default_service()` is called.

| Variable | Default | Notes |
|---|---|---|
| `RESOLVER_TIMEOUT` | `5` | Per-request timeout for adapter HTTP work; clamped to `0.5 .. 15` seconds |
| `RESOLVER_CACHE_TTL` | `300` | Positive cache seconds (never past `Resolved.expires_at`) |
| `RESOLVER_NEGATIVE_TTL` | `15` | Negative cache seconds; `0` disables |
| `RESOLVER_RATE_PER_MINUTE` | `60` | Per-host token refill; `0` disables limiting |
| `RESOLVER_BURST` | `10` | Per-host bucket size |
| `RESOLVER_DIRECT_HOSTS` | curated list | Comma-separated exact/suffix hosts. **Replaces** the defaults; empty disables direct audio |
| `RESOLVER_ADAPTERS` | all importable | Comma-separated optional module names (`mediafire,drive,mega`); empty/absent auto-loads every module that exists |
| `RESOLVER_VERIFY` | `0` | `1` enables optional HEAD audio verification for Podomatic/direct (extra upstream request; off by default because the service is pass-through) |

No configuration enables private hosts or wildcard allowlists.

Tests inject a service directly instead of using environment variables:

```python
app.config["RESOLVER_SERVICE"] = ResolverService(
    adapters=[DirectResolver(allowed_hosts=["cdn.example.com"], dns=fake_dns)],
    dns=fake_dns,
    clock=fake_clock,
)
```

---

## How the pieces fit

| File | Responsibility |
|---|---|
| `core/resolver/base.py` | `Resolver` protocol, `Resolved`, typed errors, URL normalisation, `HostAllowlist`, SSRF/private checks, header sanitising, bounded timeout helper, `UrllibHTTPClient` (redirect-free, capped bodies) |
| `core/resolver/direct.py` | allowlisted `.mp3`/`.m4a` pass-through; optional referer/UA headers; optional HEAD probe |
| `core/resolver/podomatic.py` | `*.podomatic.com` enclosures and `pdcn.co/e/...` canonicalisation; rejects pages, feeds and playlists |
| `core/resolver/cache.py` | TTL + negative cache + per-host single-flight, injectable clock |
| `core/resolver/service.py` | Flask blueprint, session auth, error→status mapping, per-host token bucket, adapter registry, optional adapter discovery |
| `core/resolver/__init__.py` | Flask-free exports; lazily exposes the service names |

`core/resolver` intentionally does not import Flask at package import time, so
provider modules shared with the Kodi add-on can import `base` without pulling
in the web server.

---

## Plugging in new adapters

Mediafire, Drive and Mega belong in sibling files **implemented by other
agents**:

```
core/resolver/mediafire.py   -> class MediafireResolver
core/resolver/drive.py       -> class DriveResolver
core/resolver/mega.py        -> class MegaResolver
```

Each class implements the protocol from `base.py`:

```python
from . import base

class MediafireResolver:
    source = "mediafire"                 # stable id returned in JSON

    def __init__(self, timeout=base.DEFAULT_TIMEOUT, http=None, dns=None,
                 clock=time.time, **kwargs):
        self.timeout = base.clamp_timeout(timeout)
        self.http = http                 # base.UrllibHTTPClient, bounded + SSRF-safe
        self.dns = dns
        self.clock = clock

    def matches(self, url):
        # Cheap and side-effect free: parse/normalise, check host/path only.
        # No DNS, no HTTP, no cache access.
        ...

    def resolve(self, url):
        # May use self.http for the provider page/metadata.
        # Must raise a typed base error (NotFound, UpstreamError,
        # UpstreamTimeout, UnsupportedMedia, BlockedURL, ...), never return a
        # page URL or a URL outside the provider's direct-file host.
        return base.Resolved(
            url="https://download123.mediafire.com/.../mix.mp3",
            headers={"Referer": "https://www.mediafire.com/"},
            expires_at=self.clock() + 3600,
            source=self.source,
        )
```

Rules for adapter authors:

* `matches()` must never perform I/O; it is called before the cache and rate
  limiter on every request.
* Allowlist the provider's direct-file hosts with `base.HostAllowlist`; do not
  accept arbitrary paths. Reject page/player/playlist URLs with
  `base.UnsupportedMedia`.
* Call `base.assert_public_url(url, dns=self.dns)` before returning a URL (the
  service also re-checks, defence in depth). Never pass `allow_private`.
* Optionally expose ``self.allowlist = base.HostAllowlist([...])``. When
  present, the service verifies the resolved URL's host against it (a
  provider bug cannot return an arbitrary public host). The shipped
  Podomatic and direct adapters both expose one.
* Use `self.http` (`UrllibHTTPClient`) for metadata. It caps bodies at 1 MiB,
  clamps timeouts, refuses redirects and re-checks the destination. Do not use
  `requests`/`urllib` directly for provider pages without the same guards.
* Raise `base.NotFound` for 404/410, `base.UpstreamTimeout` for socket
  timeouts, `base.UpstreamError` for other provider failures, and
  `base.UnsupportedMedia` for non-media URLs. These map to the HTTP table
  above.
* Keep signed query strings out of `ResolverError` messages.
* Accept `timeout`, `http`, `dns`, `clock` keyword arguments. The service
  auto-imports `MediafireResolver`/`DriveResolver`/`MegaResolver` from those
  module names when present and passes them (a no-argument fallback is used
  only if the constructor rejects them).
* Do not register routes, sessions or global state. One adapter = one class.

If auto-discovery is undesirable, pass instances explicitly:

```python
ResolverService(adapters=[MediafireResolver(...), DriveResolver(...),
                          PodomaticResolver(...), DirectResolver(...)])
```

Order matters: the first `matches()` wins. Put provider-specific adapters
before `direct` (the default builder already does).

---

## Kodi / player usage

Kodi has no special endpoint; it consumes the same JSON:

1. Call `GET /api/resolve?u=<url-encoded provider URL>` with the session
   cookie used by the web app.
2. On `200`, set the Kodi `ListItem` path to `url` and apply `headers` as
   `setProperty("...")`/`http-header` values in the add-on's player.
3. On `expires`, do not cache the URL past that timestamp; re-resolve on the
   next play.
4. On `400/404/429/502/504`, use `code` for the user-facing message and honor
   `Retry-After`/`retry_after` on `429`.

Players should not append their own `Referer`/`User-Agent` over the returned
headers; the adapter values are part of the contract.

---

## Tests

Offline, no DNS or network:

```sh
.venv/bin/python -m pytest -q test/test_resolver_service.py
```

Covers adapter matching/resolution with fake DNS and fake HTTP, Podomatic
page/playlist rejection, cache TTL/negative/single-flight, per-host rate
limiting, auth `401`, blocked/private hosts, every error→status mapping,
arbitrary-host rejection, the GET/POST JSON contract and no-redirect/no-store
headers. The shared `test/conftest.py` still imports the web app, so run from
the repo root (the worktree's parent `.venv` works during review).

## Non-goals

* Media byte proxying, range forwarding or pre-buffering — use
  `/api/stream`/Downloads for that.
* Provider OAuth/cookie jars, Mediafire page scraping, Drive quota handling and
  Mega encryption — implemented by the corresponding adapters, not here.
* A public unauthenticated resolve API or a wildcard host allowlist.
* Deploying. The branch is committed locally only.
