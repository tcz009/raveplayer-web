# MEGA resolver

Status: adapter for the Rave resolver service, extended by the decrypting
stream proxy. Hardened on branch `feat/mega-hardening`, worktree
`../rave-player-worktrees/mega-hardening`, base `6eb01db` (1.6.64).

Files:

- `core/resolver/mega.py` — parser, MEGA API client, attribute decryption,
  errors, cache, single-flight.
- `core/resolver/mega_stream.py` — AES-CTR decrypting stream proxy and signed
  links; see [MEGA-STREAM.md](MEGA-STREAM.md).
- `core/resolver/__init__.py` — package marker only (no imports).
- `test/test_mega_resolver.py` — offline resolver tests.
- `test/test_mega_stream.py` — offline stream-proxy tests.
- `docs/MEGA-RESOLVER.md` — this document.
- `docs/MEGA-STREAM.md` — the `/api/mega/stream` contract.

`app.py` now registers the resolver and Mega-stream blueprints. The resolver
still never fetches media bytes; the stream proxy is a separate module.

## 1. What it does

MEGA file links are not ordinary HTTP URLs. The path carries an 8-character
file handle, while the fragment carries a 32-byte file key that a browser
never sends to MEGA's servers. The adapter:

1. parses a MEGA file link and validates its host/key without network I/O;
2. calls MEGA's public API for a short-lived storage URL;
3. decrypts the file attributes (`at`) with the fragment key, which proves the
   key matches the file and yields the real filename;
4. returns the storage URL plus the headers a client should use, with a short
   cache TTL and a negative cache for dead links.

Supported link forms:

```text
https://mega.nz/file/<handle>#<key>
https://mega.nz/#!<handle>!<key>              (legacy)
http://…, https://www.mega.nz/…, mega.co.nz, mega.io
```

Folder links (`https://mega.nz/folder/…` and `https://mega.nz/#F!…!…`) are
recognised and rejected with `Unsupported` before any API call. Password-
protected links, account-only nodes, and multi-file folders are out of scope.

## 2. Integration contract

```python
from core.resolver import mega

if mega.supports(link):
    try:
        result = mega.resolve(link)
    except mega.Unsupported:
        ...   # not ours / folder: fall back to another resolver
    except mega.NotFound:
        ...   # dead, expired, or blocked link
    except mega.RateLimited as exc:
        ...   # retry later; exc.retry_after may be set
    except mega.FormatChanged:
        ...   # MEGA changed something; do not retry blindly
    except mega.UpstreamError:
        ...   # network/5xx; normal transient-error handling
```

`resolve()` returns:

```python
{
    "url": "http://gfs208n127.userstorage.mega.co.nz/dl/…",
    "headers": {"User-Agent": "…"},
    "expires": 1794217251,       # absolute POSIX timestamp
    "source": "mega",
    "name": "set.mp3",           # decrypted `n` attribute, or None
    "size": 312896,              # plaintext size from the API, or None
}
```

`name`/`size` are additive (1.6.60 stream work); the original four keys are
unchanged. The decrypting stream proxy uses them for ``Content-Type`` and
``Content-Length``.

The storage URL is a short-lived capability and may return 403 after a few
uses.  The earlier “roughly two-use” claim did not reproduce on 2026-11-09 (one
fresh URL served 10/10 sequential Range requests), so do not depend on a fixed
use count.  Call `mega.invalidate(link)` before a new playback, or let
`/api/mega/stream` do it; that endpoint opens the URL once per playback and
re-resolves exactly once on a 403, within a per-link refresh budget.  See
[MEGA-STREAM.md](MEGA-STREAM.md).

`expires` is `int(time.time()) + URL_TTL`. The URL is safe to hand to a
download proxy or a player; the resolver itself never fetches it. `headers`
contains only a browser-like `User-Agent`; MEGA's storage nodes do not need
cookies or a referer.

Also public:

- `mega.parse_link(value) -> MegaLink` — `handle`, canonical `key`,
  `key_bytes`, `canonical`, `cache_key()`.
- `mega.supports(value) -> bool`.
- `mega.decrypt_attributes(at_b64, key) -> dict` — used by tests and any
  caller that needs the filename/thumbnail handle.
- `mega.derive_content_key(key) -> (aes_key, ctr_iv)` — for the eventual
  downloader; not used to resolve URLs.
- `mega.clear_memory()` — drop this worker's URL/negative cache.
- `mega.invalidate(link)` — drop one link's entry (after a storage 403, or
  before a new playback).
- `mega.resolve(..., transport=..., sleep=..., now=...)` — injection points
  for offline tests; production callers use the defaults.

The module is thread-safe. All callers share one process-local cache and one
single-flight entry per `handle:key`.

## 3. MEGA API call

One request is made:

```http
POST https://g.api.mega.co.nz/cs?id=<random-int>
Content-Type: application/json
Accept: application/json
User-Agent: <browser UA>

[{"a":"g","g":1,"p":"<handle>"}]
```

`a` = action, `g` = request the download URL, `p` = public file handle.
The `id` value is a cache-busting sequence number; it is not user-controlled.

A real response (captured 2026-11-09, token truncated):

```json
[{
  "s": 312896,
  "at": "YCcAoKWusVir1UMj2zXh-LDnRDMFmQI8ZtJvieJT7JVJj9fZtybnAKpQ…",
  "g": "http://gfs208n127.userstorage.mega.co.nz/dl/5ULVebQa3z6…",
  "msd": 1,
  "ip": ["185.206.26.37", "2a0b:e42:1::37"],
  "fh": "xz6PxE57aGI"
}]
```

The adapter uses:

| field | meaning |
| --- | --- |
| `g` | ephemeral download URL (opaque capability, usually plain `http`) |
| `at` | AES-CBC encrypted attributes (`MEGA{json}` after decryption) |
| `s` | size in bytes (validated if present) |
| `ip` | the egress IP(s) MEGA bound the URL to (informational) |

### `g` is not AES-encrypted

This is worth stating because several descriptions of MEGA call it an
"encrypted download URL". In the public-file `a: "g", g: 1` response, `g` is a
plain capability URL. The only encrypted field is `at`. The URL must still be
treated as opaque:

- keep it byte-for-byte; do **not** `urllib.parse.quote()` it again;
- preserve query parameters/nonces (`?n=…&…`) and percent escapes;
- it is often `http://`, not `https://`; the same token over HTTPS returned
  HTTP 403 in live testing, so do not upgrade the scheme;
- it can be bound to the public IP that requested it and to a short lifetime;
- it is usually consumed by the caller, not by this resolver.

Live testing on 2026-11-09 did **not** reproduce a fixed two-use limit: a
fresh storage URL served ten sequential `Range: bytes=0-0` requests with 206.
A 403 can still happen (expiry, edge rotation, IP binding), so treat the URL as
a per-playback capability and re-resolve once: call `mega.invalidate(link)`
before a new playback, or fetch it once through the stream proxy and let the
proxy serve client range requests. See "Limitations" below.

The adapter accepts an HTML-escaped URL (`&amp;`) and a fully percent-encoded
URL (`http%3A%2F%2F…`) and unescapes them exactly once. Normal URLs are not
re-encoded.

## 4. Crypto scheme

### Link key

The fragment is unpadded base64url. Decoding it yields 32 bytes:

```text
raw = bytes[0..31]
k   = raw[0..15]       # first 128-bit half
n   = raw[16..31]      # second 128-bit half (nonce)
```

MEGA derives two values:

```text
attribute_key = k XOR n                      # 16 bytes, AES-128 key
content_iv    = raw[16..23] + 8 zero bytes   # 16-byte AES-CTR IV
```

The content AES key is the same `k XOR n`; the file body uses AES-128-CTR
with `content_iv`. This adapter only decrypts attributes, but
`derive_content_key()` exposes the derivation so the downloader does not
re-implement it.

### Attributes (`at`)

```text
ciphertext = base64url_decode(at)             # no padding required
plaintext  = AES-128-CBC-decrypt(attribute_key, IV=00…00, ciphertext)
plaintext  = plaintext.rstrip(b"\x00")        # NUL padding, not PKCS#7
assert plaintext.startswith(b"MEGA")
json       = json.loads(plaintext[4:])
```

Example from the known fixture:

```text
key         = Fje3cxhPb4NkBJ936KVQjPNdSwslGqiboa8IYb05t5Q
raw key     = 1637b773184f6f8364049f77e8a5508cf35d4b0b251aa89ba1af0861bd39b794
attribute_key = e56afc783d55c718c5ab9716559ce718
content_iv    = f35d4b0b251aa89b0000000000000000
at plaintext  = MEGA{"c":"VgwgxFcP_SkwurDGStjnawTqJ_Bn","n":"Quick Brown Fox 4K v3 (Use with 4-4-4 Chroma Subsampling).png"}
```

A wrong fragment key normally fails the `MEGA` magic check; the adapter reports
`FormatChanged` because the same symptom can mean MEGA changed its scheme.
`test_nist_aes_cbc_known_answer` separately checks the AES-128-CBC primitive
against the NIST SP 800-38A vector.

## 5. Error modes

All errors derive from `MegaError` (alias `ResolverError`). They carry `code`
(an HTTP-like status), `kind`, and `message`.

| exception | `code` | when |
| --- | --- | --- |
| `Unsupported` | 400 | not a MEGA file link, folder link, bad/missing key, wrong host, credentials or odd port |
| `NotFound` | 404 | API `ENOENT`/`EEXPIRED`/`EACCESS`/`EBLOCKED`, HTTP 404/403/410 |
| `RateLimited` | 429 | HTTP 509/429, `EAGAIN`, rate/quota/connection limits |
| `FormatChanged` | 502 | invalid JSON/shape, missing `g`/`at`, bad crypto, non-allowlisted download URL, unexpected status |
| `UpstreamError` | 502 | timeout, DNS/TLS/socket failure, HTTP 5xx, `EINTERNAL`/`EFAILED` |

MEGA API error-code mapping:

| code | name | mapping |
| --- | --- | --- |
| `-1` | EINTERNAL | `UpstreamError` |
| `-2` | EARGS | `FormatChanged` |
| `-3` | EAGAIN | `RateLimited` (retryable) |
| `-4` | ERATELIMIT | `RateLimited` (retryable) |
| `-5` | EFAILED | `UpstreamError` |
| `-6` | ETOOMANY | `RateLimited` (retryable) |
| `-8` | EEXPIRED | `NotFound` |
| `-9` | ENOENT | `NotFound` |
| `-11` | EACCESS | `NotFound` |
| `-14` | EKEY | `FormatChanged` |
| `-16` | EBLOCKED | `NotFound` |
| `-17` | EOVERQUOTA | `RateLimited`, **not** busy-retried, 300 s hint |
| `-18` | ETEMPUNAVAIL | `RateLimited` (retryable) |
| `-19` | ETOOMANYCONNECTIONS | `RateLimited` (retryable) |
| `-24` | EGOINGOVERQUOTA | `RateLimited`, **not** busy-retried, 300 s hint |
| other | — | `FormatChanged` |

Rate limiting is bounded:

- up to 3 attempts per resolve;
- exponential backoff `0.5 s`, then `1.0 s` (a `Retry-After` header is
  honoured, capped at 5 s);
- whole resolve deadline 15 s; each HTTP request is at most 8 s and is
  shortened to the remaining budget;
- `RateLimited.retry_after` carries the server hint when present;
- quota (`EOVERQUOTA`/`EGOINGOVERQUOTA`) is not retried in a tight loop.

## 6. Cache and single-flight

| entry | TTL |
| --- | --- |
| successful URL | `URL_TTL` = 20 min (ceiling; usage cap usually dominates) |
| `NotFound` | `NEGATIVE_TTL` = 2 min |
| `RateLimited` | `retry_after` or 60 s, capped at 5 min |
| `Unsupported`, `FormatChanged`, `UpstreamError` | not cached |

The cache key is `handle:key`, so the same file through the new and legacy URL
forms shares one entry, while a different key is a different entry.

Single-flight is per cache key: if six requests miss at once, one API request
is made and all six callers receive the same result or the same typed error.
The cache stores copies of the result/headers so a caller mutating its dict
cannot poison another caller. The cache is per process, not shared across
gunicorn workers.

If a cached URL later returns 403, call `mega.invalidate(link)` and resolve
again (once; do not loop). `clear_memory()` is process-wide and intended for
tests/manual recovery, not for one hot link.

Because a storage URL is a short-lived capability, a URL cache is only safe
when the caller re-resolves after a 403. The Rave stream proxy fetches the URL
once and serves its own clients from that stream, so its clients do not consume
storage uses per range request. If the parent wants one fresh URL per playback,
call `invalidate()` immediately before `resolve()`.

## 7. Security / allowlists

Every network boundary is deny-by-default:

- **Link hosts:** only `mega.nz`, `www.mega.nz`, `mega.co.nz`,
  `www.mega.co.nz`, `mega.io`, `www.mega.io`.
- **API host:** fixed constant `g.api.mega.co.nz`; the URL is never built from
  request data (the handle only goes into the JSON body).
- **Download URL host:** exact `g.api.mega.*`/`userstorage.mega.*`, or a
  subdomain of `.userstorage.mega.co.nz`, `.userstorage.mega.nz`,
  `.userstorage.mega.io`. IP literals, credentials, fragments, non-HTTP
  schemes, lookalike domains, and storage ports other than 80/443 are
  rejected.  A trailing dot is normalised before the suffix check.
- **No open proxy:** the module never fetches the returned storage URL and
  never accepts an arbitrary URL to fetch. It is not an HTTP proxy.
- **No redirects:** the API opener installs a redirect handler that refuses
  every 3xx, so a compromised/MITM API cannot bounce the server elsewhere.
- **No environment proxy:** `ProxyHandler({})` deliberately ignores
  `HTTP_PROXY`/`HTTPS_PROXY` for API calls.
- **Bounded I/O:** 8 s request timeout, 15 s end-to-end deadline, 64 KiB
  response cap, gzip expansion cap.

Note: the download URL is a capability. It is returned to the caller and may
appear in logs if the caller logs URLs; redact its path/query in production
logs. The caller's own SSRF/range checks still apply if it proxies the URL.

## 8. Limitations

- **Folders:** not supported. A folder link is `Unsupported`; resolving a file
  inside would require listing nodes and a different key-derivation path.
- **Password-protected links:** not supported (no `p`/password key handling).
- **Accounts/sessions:** no login, no `k`/`ok` handling, no account quota.
- **Quota and IP limits:** MEGA returns 509/EAGAIN/EOVERQUOTA on shared or
  busy egress IPs. This adapter backs off briefly and then surfaces
  `RateLimited`; a busy host may still fail. Storage URLs are often bound to
  the requesting public IP, so a URL resolved through one egress may not work
  through another.
- **Short-lived capability:** a storage URL can return 403 after expiry, edge
  rotation or IP binding.  The old “two-use” limit did not reproduce on
  2026-11-09 (10/10 sequential Range requests succeeded), so do not depend on a
  fixed use count.  Re-resolve once on 403 within the stream proxy's per-link
  refresh budget; the 20-minute cache TTL is only a ceiling.
- **Short lifetime:** storage URLs also age out. A 403 from storage should
  trigger one `invalidate()` + re-resolve, not an unbounded retry loop.
- **HTTP storage:** MEGA commonly returns plain `http://gfs…` URLs. The token
  may not work over HTTPS; do not force an upgrade.
- **No content decryption inside the adapter:** the resolver returns a URL.
  `core/resolver/mega_stream.py` is the component that opens that URL once
  and serves decrypted AES-128-CTR bytes.
- **Filename/size are metadata, not the media:** `name`/`size` are now part
  of the result for the stream proxy; use `decrypt_attributes()` if you also
  need the thumbnail handle `c`.
- **Per-worker cache:** multi-worker deployments make one API call per worker.
  A shared cache (Redis) is a future change.
- **Dependency:** `pycryptodome` is required for attribute decryption and
  stream decryption; it is pinned in `requirements.txt`
  (`pycryptodome>=3.20,<5`). Without it, parsing works and `resolve()` raises
  a clear `MegaError` (code 500) at the decryption step.
- **MEGA terms:** use within MEGA's terms and do not hammer the API; this
  adapter makes one request per cache miss/resolve.

## 9. Tests

```bash
# from the mega-resolver worktree, using the web repo's venv
"../../Rave Player/.venv/bin/python" -m pytest -q test/test_mega_resolver.py
```

Result: `105 passed` (offline; no network, no live MEGA calls).

Coverage:

- new/legacy URL forms, percent-encoded fragment keys, trailing slash, query;
- lookalike/private/credentialed hosts, bad handles/keys, folders;
- real attribute vector, XOR-half key derivation, NIST AES-CBC KAT, bad magic,
  bad JSON, bad base64, block alignment, wrong key;
- API success payload/contract, URL query/nonce preservation, HTML and
  percent-escaped URLs, storage-host allowlist/private-IP rejection;
- JSON and HTTP error mapping, EAGAIN backoff order, 509 bounds, Retry-After,
  quota non-retry, network error;
- cache reuse/expiry, targeted invalidation, negative cache/expiry, no
  negative cache for `FormatChanged`, per-handle entries, six-way
  single-flight success/error.

Live verification (optional, performed once; no download): the public Kodi
sample link
`https://mega.nz/file/raByiTib#Fje3cxhPb4NkBJ936KVQjPNdSwslGqiboa8IYb05t5Q`
resolved successfully on 2026-11-09. The API returned a 312,896-byte file, the
decrypted name `Quick Brown Fox 4K v3 (Use with 4-4-4 Chroma Subsampling).png`,
and a storage URL on `gfs208n127.userstorage.mega.co.nz`; a `Range: bytes=0-0`
request to that URL returned HTTP 206. A second request also returned 206; a
third returned 403. The token was not downloaded in full and was allowed to
expire.

## 10. Why a minimal client instead of `mega.py`

`mega.py` is a full account client and was not installed in this venv. Pinning
it would add a larger, less-maintained dependency for a single public-link
endpoint; its public-link support and response handling vary by fork. The
minimal client here is about 1,000 lines including comments, has no global state
beyond the resolver cache, and exposes the documented six-key result. If the
parent prefers `mega.py`, only `_request_once`/`_post_json` need to be swapped;
the parser, crypto, errors, cache, and tests remain valid.

## References

- MEGA security whitepaper (public link key hierarchy):
  <https://mega.nz/SecurityWhitepaper.pdf>
- MEGA SDK error enum (`API_ENOENT`, `API_EAGAIN`, …):
  <https://github.com/meganz/sdk/blob/master/include/megaapi.h>
- Public reference implementation of the same attribute scheme (megadown):
  <https://github.com/tonikelope/megadown>
- Rust public-link implementation used as a cross-check:
  <https://docs.rs/megalib/0.1.1/src/megalib/public.rs.html>
- Download-URL expiry discussion:
  <https://github.com/qgustavor/mega/issues/30>
- Kodi samples page containing the live fixture link:
  <https://kodi.wiki/view/Samples>
