# MEGA decrypting stream proxy

Status: web-first `/api/mega/stream` implemented and hardened on branch
`feat/mega-hardening` (worktree `../rave-player-worktrees/mega-hardening`,
base `6eb01db` / 1.6.64).  No Kodi add-on files are changed and nothing is
deployed.  The hardening pass addressed the P2 findings from the 1.6.64 Mega
stream security audit: opaque signed URLs, request rate limits, composed
stream caps, 206 header hardening, and a positive public-IP policy.

Files:

- `core/resolver/mega_stream.py` — stream engine, range mapping, AES-CTR
  decryption, signed tokens, Flask blueprint.
- `core/resolver/mega.py` — resolver (now also returns decrypted `name` and
  plaintext `size`).
- `test/test_mega_stream.py` — offline tests.
- `app.py` — registers the blueprint and lets a valid signed token through the
  `/api/mega/stream` auth check.
- `docs/MEGA-RESOLVER.md` — resolver contract.

## 1. Why this exists

`/api/resolve` returns MEGA's storage URL, but that URL is not playable audio:

- the body is **AES-128-CTR ciphertext**;
- the URL is an ephemeral capability (it can return 403 after expiry, edge
  rotation or IP binding; the old “two-use” claim was not reproducible);
- the content key lives only in the link fragment, which a browser does not
  send to a server.

`/api/mega/stream` resolves the link once, opens the storage URL once,
decrypts the stream with AES-CTR and serves plaintext audio with byte-range
support.  A normal `<audio>` element, Kodi's player, VLC or a TV can seek.

## 2. Endpoints

```http
GET  /api/mega/stream?e=<expiry>&t=<opaque token>
HEAD /api/mega/stream?e=<expiry>&t=<opaque token>
GET  /api/mega/stream?u=<mega.nz file link>          # session-cookie clients

POST /api/mega/sign      {"url": "<mega.nz file link>"}   # preferred
GET  /api/mega/sign?u=<mega.nz file link>                  # deprecated
```

`u` is the public MEGA file link, **percent-encoded**, and is only needed when
the caller authenticates with the web session cookie:

```text
https://mega.nz/file/raByiTib#Fje3cxhPb4NkBJ936KVQjPNdSwslGqiboa8IYb05t5Q
```

The `#` must be sent as `%23`; otherwise the HTTP client treats the key as a
fragment and never sends it to the server.

A URL minted by `/api/mega/sign` no longer contains `u`: the canonical link is
encrypted into `t`, so the query string carries only ciphertext and an expiry.
`/api/resolve` returns the same opaque `stream` path for MEGA results.

### Auth

The endpoint behaves like every other `/api/*` route: a valid web session
cookie is accepted.  For players that cannot hold a cookie, `/api/mega/sign`
(which itself requires the session) mints a short-lived opaque token.  **Use
POST** so the MEGA link is in the body, not the request line:

```http
POST /api/mega/sign
Content-Type: application/json

{"url": "https://mega.nz/file/raByiTib#Fje3..."}
```

```json
{
  "ok": true,
  "url": "/api/mega/stream?e=1794220000&t=<ciphertext>",
  "path": "/api/mega/stream?e=1794220000&t=<ciphertext>",
  "absolute_url": "http://rave.local:8080/api/mega/stream?e=...&t=...",
  "expires": 1794220000,
  "source": "mega"
}
```

`GET /api/mega/sign?u=...` still works for older clients but adds a
`Warning: 299` and `Deprecation: true` header and is logged as deprecated.
`absolute_url` uses the request's `Host`; behind a reverse proxy configure the
external base URL on the player side rather than trusting the header.  `path`
is authoritative.

`/api/resolve` also adds a `stream` field (the signed relative path) when its
result source is `mega`, so the web UI can switch from the unplayable direct
storage URL to the proxy without a second round trip.

### Token model

- **Opaque and encrypted.**  The canonical link (handle **and** 32-byte key) is
  encrypted with AES-256-GCM under a dedicated key derived from the token
  secret; the expiry is authenticated as additional data.  The URL contains
  only `e=<expiry>&t=<ciphertext>` — the permanent MEGA capability is never in
  the query string, browser history or a default proxy access log.
- Default lifetime **2 hours** (`RAVEWEB_MEGA_TOKEN_SECONDS`, max 7 days).
- A token is bound to one canonical link (handle + key, normalised).  Passing a
  mismatched legacy `u` alongside the token fails closed.
- Stateless and reusable until expiry: seeking or a second Range request does
  not invalidate it.  It is not a single-use nonce.
- **No revocation.**  There is no server-side token store and no per-token
  kill switch.  Shorten `RAVEWEB_MEGA_TOKEN_SECONDS` to reduce exposure;
  rotating `RAVEWEB_MEGA_TOKEN_SECRET` (or `SECRET_KEY` when the token secret is
  derived) invalidates every outstanding token, but sessions rotate with it
  unless a separate token secret is configured.
- Secret: `RAVEWEB_MEGA_TOKEN_SECRET` when set; otherwise a dedicated subkey
  derived from the Flask `SECRET_KEY`.  Set the env var explicitly when
  multiple gunicorn workers/containers must validate each other's tokens
  (derived subkeys match only if `SECRET_KEY` matches, which is normally true,
  but an explicit media-token secret is easier to rotate independently).
- If neither is configured, `/api/mega/sign` returns `503 not_configured` and
  the session-cookie path still works.
- Expired and >7-day tokens are rejected; GCM authentication rejects a changed
  expiry or ciphertext.  The token is never logged by this code.

A media player URL therefore looks like:

```text
http://192.168.1.20:8080/api/mega/stream?e=1794220000&t=AbCdEf...
```

## 3. Response

```http
HTTP/1.1 200 OK
Content-Type: audio/mpeg
Content-Length: 312896
Accept-Ranges: bytes
Cache-Control: no-store
X-Content-Type-Options: nosniff
Content-Disposition: inline; filename="set.mp3"; filename*=UTF-8''set.mp3
Cross-Origin-Resource-Policy: same-origin
```

- `Content-Type`: from the decrypted filename extension — `.mp3` →
  `audio/mpeg`, `.m4a`/`.mp4` → `audio/mp4`, `.wav` → `audio/wav`, plus
  `.aac`, `.flac`, `.ogg`, `.opus`, `.webm`; unknown → `application/octet-stream`.
- `Content-Length`: the plaintext length when MEGA reported the size (or the
  requested range length).
- `Accept-Ranges: bytes` is always present.
- `Content-Disposition` is optional and safe-filename-escaped.
- `HEAD` performs the resolve (no storage GET, so it does **not** consume a
  storage use) and returns the same metadata.  It never has a body.  HEAD is
  still charged to the request-rate bucket and the shared stream budget: it
  can enumerate distinct links and must not bypass the concurrency cap.

### Range requests

```http
Range: bytes=1001-1010
```

```http
HTTP/1.1 206 Partial Content
Content-Range: bytes 1001-1010/4096
Content-Length: 10
```

Supported forms: `bytes=start-end`, `bytes=start-` and `bytes=-suffix`.
Multiple ranges and malformed ranges return `416` with
`Content-Range: bytes */<size>` when the size is known (the streamer resolves
once to obtain the size for a malformed range).  A Range header with an
unknown unit (for example `items=0-1`) is ignored per RFC 7233 and the full
`200` body is served.

AES-CTR is a stream cipher: plaintext byte `N` is ciphertext byte `N` XOR the
keystream byte at position `N`.  If a client asks for `[start, end]`, the proxy:

1. requests the storage URL from the **previous 16-byte boundary**
   (`block = start - start % 16`) so pycryptodome can start the counter at
   `block // 16`;
2. initialises AES-CTR with `nonce = key[16:24]`, `initial_value = block // 16`
   (counter increments once per 16-byte block, 128-bit big-endian);
3. discards the `start % 16` bytes the storage node sent before `start`;
4. decrypts the remaining ciphertext and stops after `end - start + 1` bytes.

If the storage node ignores `Range` and returns `200` with the whole body, the
proxy skips and decrypts from the requested offset instead.  The skip counts
against the per-stream byte cap, so a request near the end of a large file
cannot make the proxy decrypt an unbounded prefix.

If the storage node returns `206` **without a parsable `Content-Range`**, the
proxy does **not** assume the body starts at the client's `start`.  It decrypts
from the only offset it actually requested, `block_start = start - start % 16`;
if that still cannot be reconciled (a server start beyond `start`) the request
fails with `502`.  This prevents a silent one-byte plaintext shift.  A
`Content-Range` start beyond the requested block is a `502`, never a guess.
`Content-Range` to the client always describes plaintext offsets.

## 4. Storage capability rotation

One stream request makes **one** storage HTTP request.  It stays on that
connection and decrypts until the client closes or the range ends.

The older docs described the storage URL as a two-use capability.  That did
not reproduce on 2026-11-09 (one fresh URL served 10/10 sequential Range
requests), so do not rely on a two-use limit for rotation.  Treat the URL as a
short-lived capability controlled by MEGA's edge.

On the first `403` the proxy:

1. closes the failed response;
2. calls `mega.invalidate(link)` to drop the cached capability;
3. resolves the link **once** more;
4. opens the fresh URL once.

A second `403` is returned to the client as `403 upstream_refused`.  There is
no loop and no re-resolve on other status codes.  Re-resolves are also budgeted
per canonical link (`RAVEWEB_MEGA_RERESOLVE_PER_MINUTE` / `_BURST`, default
6/min with burst 2): once the budget is exhausted the request returns `429
rate_limited` with `Retry-After` instead of doubling MEGA API calls again.

## 5. Errors

| HTTP | `code` | When |
|---|---|---|
| `400` | `invalid` | missing `u` / `url` |
| `400` | `unsupported` | not a MEGA file link (folder links, non-Mega hosts) |
| `401` | `unauthorized` | no session and no valid token |
| `403` | `upstream_refused` | MEGA refused the URL again after one re-resolve |
| `404` | `not_found` | MEGA says the file is gone/expired/access-blocked |
| `416` | `range_not_satisfiable` | bad or unsatisfiable `Range`; includes `Content-Range` |
| `429` | `rate_limited` | request-rate or 403 re-resolve budget exceeded, or MEGA API throttling; includes `Retry-After` |
| `502` | `upstream` | storage/API failure, unsafe URL, malformed response, unreconcilable 206 offset |
| `503` | `busy` | shared stream budget or the narrower MEGA cap is full |
| `503` | `not_configured` | `/api/mega/sign` without a token secret |
| `504` | `timeout` | storage connect timed out |

Error bodies are JSON `{"error": "...", "code": "..."}` and never contain the
link key or a signed storage token.

## 6. Security model

This is **not** an open proxy.

1. **Only MEGA file links.** `mega.parse_link` rejects every host except the
   exact official Mega domains, rejects folders, credentials, control
   characters and bad keys, and does so before any network I/O.  A caller
   cannot supply an arbitrary `u`.
2. **Only MEGA storage is opened.** The URL comes from the resolver and is
   re-validated by `mega.validate_download_url` (scheme, no credentials, no
   fragment, exact `*.userstorage.mega.co.nz|.nz|.io` suffix or the API host).
   Storage ports are restricted to **80/443**; a trailing dot is normalised
   before the suffix check so it cannot bypass the allowlist.
3. **Pinned public address.** `open_storage` resolves the storage host itself
   and accepts only `is_global` answers, explicitly rejecting CGNAT
   `100.64.0.0/10`, IPv6 site-local `fec0::/10`, IPv4-mapped private addresses
   and mixed public/private answers.  It connects to the validated IP while
   keeping the hostname for `Host`/TLS SNI.
4. **No redirects.** A 3xx from storage is a `502`, never followed.
5. **No header passthrough.** Only `Range` is translated; the proxy sends a
   fixed `User-Agent`, `Accept: */*`, `Accept-Encoding: identity` and
   `Connection: close`.  No cookies, referer, authorization or caller headers
   reach MEGA.
6. **No plaintext/keys in logs.** Log lines contain only status/class names.
   The storage URL token, link fragment key and decrypted bytes are never
   formatted into a log record.
7. **Request-rate caps.** `/api/mega/sign` and `/api/mega/stream` each have a
   token bucket keyed by signed-in identity, or client IP when anonymous.
   Defaults: sign 30/min burst 10, stream 600/min burst 60.  Excess returns
   `429` with `Retry-After`.  Buckets are per process.
8. **Composed concurrency cap.** A single per-process budget
   (`RAVEWEB_STREAM_SLOTS`, default `8 - 2 = 6`) is shared by `/api/stream`
   and `/api/mega/stream`, so 6 generic + 4 MEGA streams can no longer occupy
   an 8-thread worker.  `RAVEWEB_MAX_STREAMS` (generic, default 6) and
   `RAVEWEB_MEGA_STREAMS` (default 4) remain narrower per-route caps; the
   shared budget is acquired first.  HEAD is charged to both the rate bucket
   and the budget.
9. **Bounded work per stream.** `RAVEWEB_MEGA_STREAM_SECONDS` (default 4 h) and
   `RAVEWEB_MEGA_STREAM_BYTES` (default 8 GiB) still need lowering for audio
   deployments; the byte cap now covers the Range-skip loop as well as bytes
   sent.

Operational note: signed URLs no longer contain the MEGA link or key, but `t`
is still a bearer capability until expiry.  If you put a reverse proxy in front
of 8097, redact or drop the query string for `/api/mega/stream` and
`/api/mega/sign`:

```nginx
# nginx: log the path, not the signed query
map $request_uri $rave_log_uri {
    default $request_uri;
    ~^/api/mega/(stream|sign) /api/mega/$1?<redacted>;
}
log_format rave '$remote_addr "$request_method $rave_log_uri" $status';
access_log /var/log/nginx/rave.log rave;
```

```caddy
# Caddy: disable the access log for the two capability paths
@mega path /api/mega/stream* /api/mega/sign*
handle @mega {
    log {
        output discard
    }
    reverse_proxy 127.0.0.1:8097
}
```

Application logs from this code contain only statuses, class names and public
handles; they do not contain `u`, `t`, the file key or plaintext.

## 7. Kodi usage

No Kodi add-on change is part of this branch; the add-on can point its player
at the proxy URL.

1. While the add-on still has the web session (or via a small authenticated
   helper call), request (POST preferred; GET is deprecated):

   ```http
   POST /api/mega/sign
   Content-Type: application/json

   {"url": "<mega link>"}
   ```

2. Read `url`/`absolute_url` from the JSON and give it to the player as the
   ListItem path / `setResolvedUrl` URL:

   ```python
   # inside the add-on, after an authenticated GET to /api/mega/sign
   token_url = payload["absolute_url"]  # or web_base + payload["path"]
   list_item.setPath(token_url)
   xbmcplugin.setResolvedUrl(handle, True, list_item)
   ```

3. Set `Content-Type` hint from the URL's file extension if the player needs
   one; the server also sends it.

`/api/resolve` is the shorter path when the add-on already calls it: for a
MEGA link the JSON now includes `stream` (relative signed path), so
`web_base + payload["stream"]` is playable immediately.  `url` in that
response remains the raw storage URL and should **not** be played.

### LAN / Tailscale

`absolute_url` is built from the request's `Host` header.  From a Shield or TV
on the LAN, mint the token with the LAN base URL (`http://192.168.1.20:8080`)
and use that host; `127.0.0.1` minted on the server is not reachable from the
TV.  Over Tailscale use the MagicDNS name or `100.x.y.z` address and ensure the
Flask/gunicorn bind address and firewall allow the tailnet.  The stream itself
is plain HTTP; put it behind the existing TLS reverse proxy if the network is
not trusted, with query redaction (see §6).  A token is valid for two hours by
default; re-sign when `expires` passes.  There is no revocation: rotate
`RAVEWEB_MEGA_TOKEN_SECRET` to invalidate every outstanding URL.

## 8. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `RAVEWEB_MEGA_TOKEN_SECRET` | derived from `SECRET_KEY` | token master secret (AES-GCM key derivation) |
| `RAVEWEB_MEGA_TOKEN_SECONDS` | `7200` | signed-link lifetime (max 7 days) |
| `RAVEWEB_MEGA_STREAMS` | `4` | narrower per-process MEGA stream cap |
| `RAVEWEB_STREAM_SLOTS` | `6` | total per-process stream budget shared with `/api/stream` |
| `RAVEWEB_STREAM_WORKERS` / `RAVEWEB_STREAM_RESERVE` | `8` / `2` | default slot calculation when `RAVEWEB_STREAM_SLOTS` is unset |
| `RAVEWEB_MAX_STREAMS` | `6` | narrower generic-proxy cap |
| `RAVEWEB_MEGA_SIGN_RATE_PER_MINUTE` / `_BURST` | `30` / `10` | sign request bucket (0 disables) |
| `RAVEWEB_MEGA_STREAM_RATE_PER_MINUTE` / `_BURST` | `600` / `60` | stream request bucket (0 disables) |
| `RAVEWEB_MEGA_RERESOLVE_PER_MINUTE` / `_BURST` | `6` / `2` | 403 re-resolve budget per link |
| `RAVEWEB_MEGA_STREAM_SECONDS` | `14400` | per-stream deadline |
| `RAVEWEB_MEGA_STREAM_BYTES` | `8 GiB` | per-stream byte cap (includes Range skip) |
| `RAVEWEB_MEGA_CONNECT_SECONDS` | `10` | storage connect timeout |
| `RAVEWEB_MEGA_READ_SECONDS` | `30` | storage read timeout |

The resolver's own `URL_TTL`/cache still applies; a stream request that finds a
cached URL uses it, and a 403 rotates it.

## 9. Tests

```bash
# from the mega-stream worktree, using the web repo's venv
"../../Rave Player/.venv/bin/python" -m pytest -q \
  test/test_mega_stream.py test/test_mega_resolver.py test/test_resolver_service.py
```

The stream tests are fully offline: the resolver call and the storage HTTP
client are fakes.

Coverage:

- fixed AES-CTR ciphertext fixture decrypting from offset 0 and from an
  unaligned offset, plus a long round-trip;
- range mapping at start/mid/end, open-ended and suffix ranges, block-aligned
  upstream requests, upstream-ignores-Range fallback, 416 cases;
- 403 re-resolve once, double-403 stop, and the two-use pattern (the rotation
  still works, but the docs no longer rely on a two-use edge limit);
- 403 re-resolve budget exhaustion (`429`) and a byte cap that covers the
  Range-skip loop;
- `HEAD` metadata without consuming a storage use, but still taking the shared
  stream slot; the shared budget composing Mega + generic streams;
- 206 with a missing or malformed `Content-Range` decrypting from the requested
  block start, never from the client start; unknown Range units ignored;
  malformed 416s carrying `Content-Range`;
- public-address policy: CGNAT, IPv6 site-local, reserved, IPv4-mapped private
  and mixed public/private DNS answers rejected;
- Content-Type mapping, opaque token binding/expiry/tamper/mismatched `u`,
  token auth without a cookie, session auth, non-Mega rejection;
- request-rate `429` for `/api/mega/sign` and `/api/mega/stream`, the GET-sign
  deprecation warning, `/api/resolve`'s `stream` field and no-plaintext/no-key
  logging.

Live verification is deliberately not part of the suite.  A single live check
recorded before this branch showed the storage URL serving two requests and
returning 403 on the third (see `docs/MEGA-RESOLVER.md`).
