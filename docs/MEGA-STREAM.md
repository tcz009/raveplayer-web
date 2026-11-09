# MEGA decrypting stream proxy

Status: web-first `/api/mega/stream` implemented on branch `feat/mega-stream`
(worktree `../rave-player-worktrees/mega-stream`, base `0c94a5d` / 1.6.60).
No Kodi add-on files are changed and nothing is deployed.

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
- the URL is an ephemeral, roughly **two-use capability** (a third HTTP
  request returns 403);
- the content key lives only in the link fragment, which a browser does not
  send to a server.

`/api/mega/stream` resolves the link once, opens the storage URL once,
decrypts the stream with AES-CTR and serves plaintext audio with byte-range
support.  A normal `<audio>` element, Kodi's player, VLC or a TV can seek.

## 2. Endpoints

```http
GET  /api/mega/stream?u=<mega.nz file link>[&e=<expiry>&t=<token>]
HEAD /api/mega/stream?u=<mega.nz file link>[&e=<expiry>&t=<token>]

GET  /api/mega/sign?u=<mega.nz file link>
POST /api/mega/sign      {"url": "<mega.nz file link>"}
```

`u` is the public MEGA file link, **percent-encoded**:

```text
https://mega.nz/file/raByiTib#Fje3cxhPb4NkBJ936KVQjPNdSwslGqiboa8IYb05t5Q
```

The `#` must be sent as `%23`; otherwise the HTTP client treats the key as a
fragment and never sends it to the server.

### Auth

The endpoint behaves like every other `/api/*` route: a valid web session
cookie is accepted.  For players that cannot hold a cookie, `/api/mega/sign`
(which itself requires the session) mints a short-lived HMAC token:

```http
GET /api/mega/sign?u=https%3A%2F%2Fmega.nz%2Ffile%2FraByiTib%23Fje3...
```

```json
{
  "ok": true,
  "url": "/api/mega/stream?u=https%3A%2F%2Fmega.nz%2Ffile%2FraByiTib%23Fje3...&e=1794220000&t=...",
  "path": "/api/mega/stream?...",
  "absolute_url": "http://rave.local:8080/api/mega/stream?...",
  "expires": 1794220000,
  "source": "mega"
}
```

`absolute_url` uses the request's `Host`; behind a reverse proxy configure the
external base URL on the player side rather than trusting the header.

`/api/resolve` also adds a `stream` field (the signed relative path) when its
result source is `mega`, so the web UI can switch from the unplayable direct
storage URL to the proxy without a second round trip.

### Token model

- HMAC-SHA256 over `v1\n<expiry>\n<canonical mega.nz link>`.
- Default lifetime **6 hours** (`RAVEWEB_MEGA_TOKEN_SECONDS`, max 7 days).
- A token is bound to one canonical link (handle + key, normalised); using it
  with another `u` fails.
- Stateless and reusable until expiry: seeking or a second Range request does
  not invalidate it.  It is not a single-use nonce.
- Secret: `RAVEWEB_MEGA_TOKEN_SECRET` when set; otherwise a dedicated subkey
  derived from the Flask `SECRET_KEY`.  Set the env var explicitly when
  multiple gunicorn workers/containers must validate each other's tokens
  (derived subkeys match only if `SECRET_KEY` matches, which is normally true,
  but an explicit media-token secret is easier to rotate independently).
- If neither is configured, `/api/mega/sign` returns `503 not_configured` and
  the session-cookie path still works.
- Tokens are compared with `hmac.compare_digest`; expired and >7-day tokens
  are rejected.  The token is not logged by this code.

A media player URL therefore looks like:

```text
http://192.168.1.20:8080/api/mega/stream?u=https%3A%2F%2Fmega.nz%2Ffile%2F...%23...&e=1794220000&t=...
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
- `HEAD` performs the resolve (no storage GET, so it does **not** consume one
  of the two storage uses) and returns the same metadata.  It never has a
  body.

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
`Content-Range: bytes */<size>` when the size is known.

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
proxy skips and decrypts from the requested offset instead.  `Content-Range`
to the client always describes plaintext offsets.

## 4. Two-use storage capability

One stream request makes **one** storage HTTP request.  It stays on that
connection and decrypts until the client closes or the range ends.  The
resolver's URL cache can hand the same storage URL to two playbacks; the third
open receives `403`.

On the first `403` the proxy:

1. closes the failed response;
2. calls `mega.invalidate(link)` to drop the cached capability;
3. resolves the link **once** more;
4. opens the fresh URL once.

A second `403` is returned to the client as `403 upstream_refused`.  There is
no loop, no retry budget shared between requests and no re-resolve on other
status codes.

## 5. Errors

| HTTP | `code` | When |
|---|---|---|
| `400` | `invalid` | missing `u` / `url` |
| `400` | `unsupported` | not a MEGA file link (folder links, non-Mega hosts) |
| `401` | `unauthorized` | no session and no valid token |
| `403` | `upstream_refused` | MEGA refused the URL again after one re-resolve |
| `404` | `not_found` | MEGA says the file is gone/expired/access-blocked |
| `416` | `range_not_satisfiable` | bad or unsatisfiable `Range`; includes `Content-Range` |
| `429` | `rate_limited` | MEGA API throttling; includes `Retry-After` when known |
| `502` | `upstream` | storage/API failure, unsafe URL, malformed response |
| `503` | `busy` | too many concurrent MEGA streams (default 4) |
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
3. **Pinned public address.** `open_storage` resolves the storage host itself,
   rejects private/loopback/link-local/reserved answers and connects to the
   validated IP while keeping the hostname for `Host`/TLS SNI.
4. **No redirects.** A 3xx from storage is a `502`, never followed.
5. **No header passthrough.** Only `Range` is translated; the proxy sends a
   fixed `User-Agent`, `Accept: */*`, `Accept-Encoding: identity` and
   `Connection: close`.  No cookies, referer, authorization or caller headers
   reach MEGA.
6. **No plaintext/keys in logs.** Log lines contain only status/class names.
   The storage URL token, link fragment key and decrypted bytes are never
   formatted into a log record.
7. **Concurrency cap.** `RAVEWEB_MEGA_STREAMS` (default 4) bounds how many
   decrypting streams can occupy worker threads.

Operational note: the signed URL puts the MEGA link in the query string.  A
reverse proxy that logs full request lines will therefore log the link
capability.  Strip query strings for `/api/mega/stream` in the access log, or
accept that the link is already a bearer capability (it is the same secret the
user pasted into the app).  Application logs from this code do not contain it.

## 7. Kodi usage

No Kodi add-on change is part of this branch; the add-on can point its player
at the proxy URL.

1. While the add-on still has the web session (or via a small authenticated
   helper call), request:

   ```http
   GET /api/mega/sign?u=<urlencoded mega link>
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
not trusted.  A token is valid for six hours by default, so a player can seek
and resume without re-authenticating; re-sign when `expires` passes.

## 8. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `RAVEWEB_MEGA_TOKEN_SECRET` | derived from `SECRET_KEY` | HMAC key for `/api/mega/sign` |
| `RAVEWEB_MEGA_TOKEN_SECONDS` | `21600` | signed-link lifetime (max 7 days) |
| `RAVEWEB_MEGA_STREAMS` | `4` | concurrent decrypting streams |
| `RAVEWEB_MEGA_STREAM_SECONDS` | `14400` | per-stream deadline |
| `RAVEWEB_MEGA_STREAM_BYTES` | `8 GiB` | per-stream byte cap |
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
- 403 re-resolve once, double-403 stop, and the real two-use pattern (two
  opens succeed, third 403 rotates the capability and the replay succeeds);
- `HEAD` metadata without consuming a storage use;
- Content-Type mapping, signed token binding/expiry/tamper, token auth without
  a cookie, session auth, non-Mega rejection;
- `/api/mega/sign`, `/api/resolve`'s `stream` field and no-plaintext/no-key
  logging.

Live verification is deliberately not part of the suite.  A single live check
recorded before this branch showed the storage URL serving two requests and
returning 403 on the third (see `docs/MEGA-RESOLVER.md`).
