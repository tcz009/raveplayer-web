# MEGA decrypting stream proxy — security & correctness audit (web 1.6.64)

**Status:** report only. No source, config, deploy or Kodi/Shield changes were
made. The only repository file added is this document. Live checks were
read-only HTTP/SSH; authenticated checks reused a pre-existing session cookie
(normal `last_seen` update only).

**Token-path method note:** for the token-only checks I minted short-lived
tokens *inside* the running container with a read-only `docker exec` using the
deployed `SECRET_KEY` (the secret was never printed, and no container file or
config was changed). The same checks were repeated with a signed URL returned
by the deployed `/api/resolve` and with the existing session cookie, so the
result does not depend on the minting method.

**Audited target:** `6eb01db` = tag `v1.6.64` = `origin/main`
(“1.6.64: immutable build identity (resolver + Mega stream)”).
`6eb01db` itself changes only `VERSION` and `static/sw.js`; the Mega stream code
was introduced in `06a7ae1` and merged in `1ee1029`, with the 1.6.63 error
mapping in `c70d315`. Line numbers below are from `6eb01db`.

**Live target:** NAS `thinkomv`, container `raveplayer-web`,
`GET /healthz` → `{"ok":true,"version":"1.6.64","data_writable":true}`.
Image labels: `org.opencontainers.image.version=1.6.64`,
`org.opencontainers.image.revision=6eb01db`; image built 2026-11-09 09:55 UTC.
Gunicorn: `-w 2 -k gthread --threads 8 -t 120`; `RAVEWEB_ENV=production`,
`SECRET_KEY` set, `RAVEWEB_MEGA_TOKEN_SECRET` unset (derived subkey),
`RAVEWEB_MEGA_STREAMS` unset (4 per worker), `RAVEWEB_MAX_STREAMS=6` (generic
proxy). 8097 is direct gunicorn; no Caddy/reverse proxy on that port; gunicorn
access logging is off; container logs contain no request URL or key.

**Scope:** `core/resolver/mega_stream.py`, the stream additions in
`core/resolver/mega.py`, `core/resolver/service.py`’s `stream` field,
`app.py` blueprint/registration/auth-guard changes, the stream tests, the
MEGA-stream docs, the live 1.6.64 deployment, and the Kodi-side consumer
contract that exists in the tree. Kodi/Shield were not altered.

---

## Verdict

**P0 — none. P1 security — none found.** The endpoint fails closed without a
session or a valid token, is not an open proxy, only ever opens an allowlisted
MEGA storage URL, refuses redirects, pins the storage connection to a resolved
address, and the AES-CTR/range implementation is correct against an independent
OpenSSL decrypt and against the live NAS.

**P1 (integration) — one:** the web side is ready, but **there is no Kodi
consumer for it**. The only Kodi resolver client in the tree
(`script.module.raveresolver`) still uses the old contract: it reads `url` (the
raw, encrypted, short-lived storage URL), ignores the new `stream` field, and
authenticates with `Authorization: Bearer`, which `/api/resolve` does not
accept. The released add-on 3.9.47 has no MEGA path at all. **Do not announce
“MEGA on Kodi” tonight** until that client is changed and tested.

**Safe to expose to Kodi?**
* **As an endpoint on the LAN: yes** (no P0/P1 security defect), provided 8097
  stays LAN-only, the signed URL is treated as a secret, and no query-logging
  reverse proxy is put in front of it without redaction.
* **As a working Kodi feature tonight: no.** A player that is *handed* a signed
  URL can stream and seek correctly; nothing in Kodi mints or consumes that URL
  yet.

The remaining P2s are capability leakage through URLs, missing rate limiting,
a conditional 206-header misdecrypt, and resource-cap composition; none is a
remote unauthenticated compromise.

Severity is used as: **P0** unauthenticated compromise / open proxy; **P1**
high-impact security or a blocking integration defect; **P2** medium or
defence-in-depth; **P3** low/hardening.

| # | Sev | Area | Finding |
|---|---|---|---|
| P1-1 | **P1** (integration) | Kodi contract | Existing Kodi resolver client uses raw `url`, ignores `stream`, and uses Bearer auth unsupported by `/api/resolve` |
| P2-1 | P2 | Token/privacy | Signed URL contains the full MEGA link **including the 32-byte file key**, plus a reusable 6 h bearer token; any URL log/history/Kodi log leaks the permanent file capability |
| P2-2 | P2 | Rate limiting | `/api/mega/stream` and `/api/mega/sign` have no request-rate limit; the streamer calls `mega.resolve` directly, bypassing the resolver’s token bucket |
| P2-3 | P2 (correctness) | Range | A `206` upstream response without a parsable `Content-Range` is assumed to start at the client’s `start`, not the requested block start → wrong plaintext |
| P2-4 | P2 (resource) | Concurrency | Caps are per process and do not compose with the generic proxy; `HEAD` bypasses the Mega semaphore; 4 h/8 GiB limits are long |
| P2-5 | P2 | Token lifetime | 6 h default, 7 day max, stateless, no per-token revocation or client binding; a leaked URL works from any LAN host until expiry |
| P2-6 | P2 (defence in depth) | SSRF | Public-address check is a denylist; CGNAT `100.64/10` and IPv6 site-local `fec0::/10` pass. Host suffix allowlist remains the primary barrier |
| P3-1 | P3 (correctness) | Range/RFC | Malformed-range 416 lacks `Content-Range`; unknown range units return 416 instead of being ignored; multi-range and unknown-size suffix ranges are 416 |
| P3-2 | P3 | Docs | `docs/MEGA-STREAM.md` still says branch-only/not deployed; the “two-use” storage claim did not reproduce (10/10 uses were 206) |
| P3-3 | P3 | Contract errors | `/api/resolve` reports missing/invalid MEGA key as `400 unknown` and a wrong key as `502 upstream`; stream wrong-key is `502` |
| P3-4 | P3 | Signing UX | `/api/mega/sign` accepts GET (key in query), is unrated, has no CSRF token (SameSite=Lax only), and `absolute_url` trusts `Host` |
| P3-5 | P3 | Deployment | `/healthz` exposes version but not commit; floating `pycryptodome` pin; storage URLs on any port (e.g. `:22`) and trailing-dot hosts are accepted; no HTTPS |
| P3-6 | P3 | UI | The web UI never calls `/api/mega/sign` or `/api/mega/stream`; only API clients can use the feature |

---

## P1-1 — Kodi client/contract mismatch (integration blocker)

**Where**

* `raveplayer-worktrees/resolver-module/script.module.raveresolver/resources/lib/resolver/service.py`
  * `:129` `target = str(result.get("url") or "").strip()` — selects the raw
    storage URL from `/api/resolve`.
  * `:139` expects `provider`; web returns `source`.
  * `:163` reads `expires_at`; web returns `expires` (the storage URL expiry,
    20 min), and the signed stream URL’s expiry is inside its `e` parameter.
  * The client sends `Authorization: Bearer <token>`; web `/api/resolve` is
    session-cookie only (live 401 without a cookie, 200 with one).
  * No reference to `stream` anywhere in the module.
* The released Kodi add-on in `raveplayer/` (plugin.audio.raveplayer 3.9.47)
  contains no MEGA code and does not call `/api/resolve`.

**Why it matters**

If the resolver module is wired as its own docs describe, a MEGA item resolves
to `http://gfs….userstorage.mega.co.nz/dl/…`:
1. it is AES-CTR ciphertext, not audio;
2. it is a short-lived capability (and may be IP-bound), not a stable URL;
3. the signed `stream` path that the web side added for exactly this case is
   discarded.

The result is a failed playback, not a security exposure — but it means the
feature is not consumable by Kodi tonight.

**Fix**

* In the Kodi service client, prefer `payload["stream"]` and return
  `web_base + stream` as the playable URL (the path is relative); fall back to
  `url` only for non-MEGA sources.
* Parse the stream expiry from `e` in the signed path (or have the web side
  return `stream_expires`) and re-sign before it passes.
* Either support the web session cookie in Kodi (login helper) or add a
  deliberate machine-to-machine bearer for `/api/mega/sign` and
  `/api/resolve`; do not reuse the browser session secret.
* Add a Kodi test that asserts a MEGA resolve yields a URL containing
  `/api/mega/stream?` and that a `Range` request returns `206`.
* Keep the raw `url` in the response for diagnostics but document it as
  non-playable for MEGA.

---

## P2-1 — the signed URL leaks the MEGA file key and a reusable bearer

**Where**

* `core/resolver/mega_stream.py:352-360` — `signed_path()` puts
  `u=<canonical mega link>` (which includes `#<32-byte key>`) and `t=<token>`
  in the query string.
* `core/resolver/service.py:396-400` — `/api/resolve` returns that path in
  `stream`.
* `core/resolver/mega_stream.py:969-993` — `/api/mega/sign` returns the same
  path (and `absolute_url`).
* `docs/MEGA-STREAM.md` itself notes the operational risk at §6.

**Evidence**

Live `/api/resolve` for the public PNG fixture returned a 163-character
`stream` path whose decoded `u` was the canonical link **with the key**; the
`t` token is reusable until `e` (default 6 h). The path works from any host that
can reach 8097. The deployed gunicorn has no access log and no proxy, so the
server does not currently write it; but:
* any future Caddy/nginx access log records the full query by default;
* Kodi/Shield debug logs, browser history, copied URLs, screenshots and
  `Referer`-capable clients can record it;
* the `u` key is the **original, permanent MEGA capability** — the 6 h token
  expiry does not expire the key.

**Impact**

A reader of one log line gets permanent access to the underlying MEGA file and
a 6 h proxy capability. On a shared LAN device, browser history alone exposes
it. This is a capability-confidentiality issue, not a remote auth bypass.

**Fix (preferred, in order)**

1. Make the signed URL opaque: store `{id: (canonical_link, expires)}` in a
   small server-side table (SQLite already exists for audit) and put only
   `?id=<random>&t=<hmac>` in the URL. This hides the key and enables per-token
   revocation.
2. Alternatively encrypt the canonical link into the token payload
   (AES-GCM with a dedicated media key) and keep only ciphertext in the URL.
3. Until then: use `POST /api/mega/sign` (body, not query) from clients; set a
   query-redaction rule on any reverse proxy for `/api/mega/sign` and
   `/api/mega/stream`; shorten the token TTL; treat the URL as a secret in docs
   and logs.

---

## P2-2 — no request-rate limit; resolver limiter bypassed

**Where**

* `core/resolver/mega_stream.py:581-586` — `MegaStreamer` defaults to
  `mega.resolve` directly.
* `:590-601` — only the concurrency semaphore (4) applies.
* `core/resolver/service.py:164-188` — the `TokenBucket` limiter is applied
  only inside `ResolverService.resolve`, which the stream route does not use.
* `core/resolver/mega_stream.py:969-993` — `/api/mega/sign` has no limiter.
* `:742-765` — each 403 triggers one extra resolve + storage open.

**Impact**

An authenticated user (or anyone holding a leaked 6 h token) can issue
sequential requests without limit. Each new link causes a MEGA API call; a 403
doubles it. This can consume the NAS’s MEGA quota, get its egress IP rate-
limited/blocked for everyone, and make the service unavailable. `MAX_STREAMS`
bounds concurrency, not request rate.

**Fix**

* Add a `TokenBucket` keyed by token hash / session identity / client IP, and
  a second one keyed by MEGA link, around `plan()` and `/api/mega/sign`.
* Route the resolve through `ResolverService` (which already has a limiter) or
  apply the same limiter in `MegaStreamer`.
* Return `429` + `Retry-After` on excess (the code already has the shape).
* Cap re-resolves per link/time, and log (handle only, never key/token) when
  the cap is hit.

---

## P2-3 — 206 without a parsable `Content-Range` decrypts the wrong bytes

**Where**

* `core/resolver/mega_stream.py:819-827`:
  `_content_range_start(resp, start)` returns the **client’s `start`** when the
  header is missing or malformed.
* The proxy actually requested `block_start = start - start % 16`
  (`:720-729`), so if the storage node honoured the Range but omitted
  `Content-Range`, the first bytes in the body are at `block_start`, not
  `start`.

**Evidence (offline probe, fakes only)**

```
206 without Content-Range, request bytes=17-33, body starts at 16:
  status 206 len 17 correct False head c775978a3045dd91
206 with malformed Content-Range "bytes 16-33":
  same: correct False
206 with proper Content-Range "bytes 16-33/312896":
  correct True
```

The live MEGA edge currently sends `Content-Range: bytes 16-33/312896`
(verified), so this is **latent**, not reproduced live. It would produce
silent corruption/seek failures behind an edge/CDN that omits the header.

**Fix**

* For a `206`, require a parsable `Content-Range`; otherwise fail with `502`
  (do not guess).
* If a fallback is desired, use the requested `block_start`, not `start`, and
  add a regression test for both missing and malformed headers.
* Optionally cross-check `Content-Range` total against the API `size` and
  log/`502` on a mismatch.

---

## P2-4 — concurrency caps do not compose; HEAD bypasses the Mega cap

**Where**

* `core/resolver/mega_stream.py:117` — `_SLOTS = BoundedSemaphore(MAX_STREAMS)`
  is per process (default 4).
* `:596-601` — `if not head and self._acquire_slot:` — **HEAD does not acquire
  a slot** (it still resolves through MEGA).
* Live container: 2 gunicorn workers × 8 gthread threads; generic proxy
  `RAVEWEB_MAX_STREAMS=6`; Mega default 4. 6 + 4 = 10 potential long-lived
  streams per worker against 8 threads.
* `:66-68` — 4 h deadline / 8 GiB per stream.

**Evidence (offline probe)**

```
slots 4
after 4 plans slots 0
5th 503 busy after 2.0 s
HEAD does not take slot: slots during HEAD 4
after close slots 4
```

**Impact**

An authenticated user can occupy every worker thread with streams (Mega or
Mega + generic), starving `/api/*` and login. HEADs bypass the Mega cap
entirely (cheap after the first cache hit, but still resolve work on distinct
links). The 4 h/8 GiB limits allow a slow-reading client to hold a slot for
hours. The byte cap is applied to bytes **sent**, not to the `discard` loop
(`:863-873`): if an upstream ignores Range, the proxy decrypts up to `start`
bytes before sending anything, bounded only by the 4 h deadline.

**Fix**

* Use one shared stream semaphore (or a global budget) across `/api/stream`
  and `/api/mega/stream`, sized below the gthread count; reserve at least one
  thread for the API.
* Include HEAD in the cap (or give HEAD a separate cheap rate limit), since it
  can still perform a resolve.
* Lower `RAVEWEB_MEGA_STREAM_SECONDS` / `RAVEWEB_MEGA_STREAM_BYTES` for audio
  (e.g. 1 h / 2 GiB) and document that `RAVEWEB_MEGA_STREAMS` is per worker,
  so the live total is `2 × value`.

---

## P2-5 — 6 h stateless bearer, no revocation or client binding

**Where**

* `core/resolver/mega_stream.py:71-75` — default 6 h, max 7 days.
* `:321-349` — HMAC over `v1\nexpiry\ncanonical` only; no user, IP, `jti` or
  single-use nonce.
* `:283-306` — secret is env `RAVEWEB_MEGA_TOKEN_SECRET` or a derived
  `SECRET_KEY` subkey.

**Impact**

A signed URL copied from Kodi logs/history works from any LAN host until
expiry. There is no way to revoke one URL; rotating `SECRET_KEY` logs out every
session, and setting `RAVEWEB_MEGA_TOKEN_SECRET` invalidates every outstanding
token only after a restart. This is acceptable for a LAN-only convenience but
should be explicit.

**Fix**

* Shorten the default for player URLs (e.g. 30–60 min) and have the client
  re-sign on `401`/`403`; keep the 7 day max only for deliberate offline use.
* Move to stateful opaque tokens (same change as P2-1) so one token can be
  revoked.
* Optionally bind to the client IP for LAN use (with a documented TV/mobile
  caveat), or bind to a per-device token issued by a small pairing flow.

---

## P2-6 — public-address check is a denylist, not “public only”

**Where**

* `core/resolver/mega_stream.py:450-479` — rejects
  `is_private/is_loopback/is_link_local/is_multicast/is_reserved/is_unspecified`.
* It does **not** reject `is_global == False` generally, nor `is_site_local`.

**Evidence (offline, monkeypatched DNS)**

```
cg.test -> 100.64.0.1      # CGNAT, is_global False, accepted
sl.test -> fec0::1         # IPv6 site-local, accepted
```

**Impact**

Only reachable if MEGA’s storage hostname is poisoned (the URL suffix
allowlist is the primary barrier), so this is defence in depth. But the
docstring says “reject private/mixed answers”; CGNAT and site-local are
non-public and should be rejected too.

**Fix**

```python
if (not address.is_global
        or getattr(address, "is_site_local", False)
        or address.is_multicast):
    raise MegaStreamError(...)
```

Add tests for `100.64.0.1`, `fec0::1`, `2001:db8::1`, IPv4-mapped private, and
mixed public/private answers.

---

## P3 findings

### P3-1 — Range/RFC edge semantics

* Malformed `Range` (e.g. `bytes=abc`, `bytes=10-5`, multi-range) is rejected
  **before** the resolve (`mega_stream.py:590-592`), so the `416` has no
  `Content-Range`. Live: `416` + no `Content-Range` for malformed, while
  `bytes=312896-` correctly returns `Content-Range: bytes */312896`. RFC 7233
  says a 416 **SHOULD** include `Content-Range`; include `bytes */<size>` when
  the size is known (or resolve first only for this case).
* `Range: items=0-1` (unknown unit) returns `416`; RFC 7233 says a server
  **MUST ignore** a Range with a unit it does not understand. Treat unknown
  units as “no range” (200 full) or document the deliberate deviation.
* Valid multi-range requests return `416`. A single-range proxy may ignore the
  header instead; at minimum document it. Kodi sends single ranges.
* `bytes=-N` with an unknown size returns `416` (`bounded_range` raises when
  `total is None`); acceptable but it means a suffix seek cannot work if MEGA
  omits `s`.
* `Content-Range: bytes start-end/*` can be emitted for a 206 when the size is
  unknown; valid grammar, but some clients reject non-concrete totals.

### P3-2 — documentation drift and the “two-use” claim

`docs/MEGA-STREAM.md:3-5` still says “implemented on branch `feat/mega-stream`
… nothing is deployed”, while 1.6.64 is live. The docs’ “roughly two-use”
storage-capability claim (`MEGA-STREAM.md` §4, `MEGA-RESOLVER.md` §9) did not
reproduce on 2026-11-09: one fresh storage URL served **10/10** sequential
`Range: bytes=0-0` requests with `206`. The 403 re-resolve path remains
offline-tested only; it is harmless, but do not rely on the two-use limit for
rotation. Update the status and the live claim.

### P3-3 — `/api/resolve` / stream error mapping

Live, with a session:

```
wrong 32-byte key  -> 502 {"error":"MEGA attribute magic missing ...","code":"upstream"}
missing key        -> 400 {"error":"no resolver is configured for this URL","code":"unknown"}
bad base64 key     -> 400 ... code unknown
folder / non-Mega  -> 400 ... code unknown
EBLOCKED           -> 404 ... code not_found
stream wrong key   -> 502 ... code upstream
```

`MegaResolver.matches()` calls `supports()` which returns `False` when the key
is missing/invalid, so the resolver never selects the MEGA adapter and reports
`unknown` rather than `unsupported`. A wrong key is a client error but maps to
`upstream`. This is fail-closed and not a security issue, but the status/code
is misleading for Kodi retry logic. Map “MEGA URL but bad/missing key” to
`400 unsupported`/`invalid`, and wrong key to `400` or `404` (do not treat it
as an upstream failure).

### P3-4 — signing endpoint / URL construction

* `/api/mega/sign` supports GET (`mega_stream.py:969-993`), so the MEGA key is
  in the query string at mint time as well as in the returned URL. Prefer POST
  in docs/clients.
* It has no CSRF token. `SameSite=Lax` blocks cross-site subresource/POST
  cookie sends, and the response is JSON without CORS, so this is low risk; a
  deliberate CSRF check would still be cleaner if the endpoint ever changes
  state.
* `absolute_url` is built from `request.url_root` (`:984`), i.e. the `Host`
  header. Behind a proxy this can be `http://` or the wrong host; docs already
  say to configure the base externally. Return `path` as authoritative.
* No audit event is recorded for token minting/streaming. An event with the
  handle (not key/token), result and client IP would help incident response.

### P3-5 — deployment / dependency notes

* `/healthz` reports `version` but not the image revision; the image labels do
  carry `6eb01db`. Add `revision` to `/healthz` if you want the running
  process (not `docker inspect`) to be traceable.
* `requirements.txt` has `pycryptodome>=3.20,<5`; the Docker build is not
  reproducible. Pin a version (and ideally a hash).
* `validate_download_url` accepts any port (`http://userstorage.mega.co.nz:22/x`
  is allowed) and trailing-dot hosts; the host suffix is still MEGA. Restrict
  storage ports to 80/443 and normalise the trailing dot before allowlisting
  for tighter egress policy.
* 8097 is plain HTTP on `0.0.0.0`; tokens/keys are in clear on the LAN. Keep it
  LAN-only; use the existing TLS proxy pattern if it must leave the LAN.
* `RAVEWEB_MEGA_TOKEN_SECRET` is unset, so rotating `SECRET_KEY` invalidates
  all tokens and all sessions together. Set a dedicated 32+ byte secret if
  independent rotation matters.

### P3-6 — no web UI integration

`static/app.js` has no `/api/mega/sign` or `/api/mega/stream` call; the web
player’s `/api/stream?u=` path is generic. The feature is API-only today. That
is fine for a Kodi-first rollout, but it means a user pasting a MEGA link into
the web UI will not get the proxy automatically.

---

## Verified-correct behaviour (not findings)

### HMAC token

* Construction: `HMAC-SHA256(key, "v1\n<int(expires)>\n<canonical link>")`,
  base64url without padding (`mega_stream.py:317-331`). Domain-separated from
  Flask’s session signer.
* Canonicalisation: `mega.parse_link(...).canonical` normalises
  `http/https`, `www.`, `mega.co.nz`/`mega.io`, legacy fragment form, key
  padding and a trailing slash. Offline, one token verified for all of:
  `http://mega.nz/file/H/#K`, `https://www.mega.co.nz/file/H#K`,
  `mega.nz/file/H#K=`, and `...?utm=x#K`. Same file → same capability; that is
  intentional.
* Binding: token for link A rejected for link B (different handle and
  different key). Live: cross-link use → `401`.
* Expiry: expired and `> 7 days` tokens → `401`; `e+1` tamper → `401`.
* Tamper: single-character token change → `401`.
* Comparison: `hmac.compare_digest` (`:349`); token must be ASCII and ≤128
  chars; `u` ≤4096.
* Secret: `RAVEWEB_MEGA_TOKEN_SECRET` wins; otherwise a derived subkey of
  `current_app.secret_key`; no secret → `/api/mega/sign` 503 and tokens fail
  closed (`:283-306`, `:321-327`). Offline: same `SECRET_KEY` in a second app
  validates; different secret fails; env secret overrides and invalidates the
  old token.
* Multi-worker: live image runs 2 gunicorn workers with one `SECRET_KEY` env;
  a token minted once validated across repeated requests (the derived key is
  process-independent as long as `SECRET_KEY` is shared). If someone runs
  without `RAVEWEB_ENV=production` and without `SECRET_KEY`, `auth.load_config`
  generates a random per-process secret (`core/auth.py:174-188`), so tokens
  (and sessions) would not be consistent across workers. Production deployment
  sets the secret, so this is a configuration guard, not a live defect.

### Route/auth

* Live unauthenticated `GET /api/resolve`, `/api/mega/sign`,
  `/api/mega/stream` → `401` JSON. With a valid signed token and no cookie,
  `/api/mega/stream` → `200`; with a session cookie and no token, `206` for a
  Range request.
* The app-level guard bypass is exact-path and re-validates in the blueprint
  (`app.py:896-902`, `mega_stream.py:913-918`, `:410-412`).
* Non-MEGA: `400 unsupported` with a session; `401` without (fail closed).
  Live `/api/mega/sign?u=https://example.com/a.mp3` → `400 unsupported`.
* Methods: GET and HEAD work (Flask auto-HEAD); HEAD returns the GET’s
  `Content-Length`/`Content-Range` and no body. POST with a valid token → `405`;
  POST without auth → `401` (guard first, fail closed).
* Unknown query parameters ignored; duplicate `u`/`e`/`t` are first-wins in
  both the guard and the view.
* No open proxy: the only URL opened comes from `mega.resolve` and is
  re-validated by `mega.validate_download_url` (scheme http(s), no credentials,
  no fragment, host exact/suffix allowlist `*.userstorage.mega.co.nz|.nz|.io`
  and `g.api.mega*`, IP literals rejected). Probes for `user@127.0.0.1`,
  `127.0.0.1#@…`, `evil-userstorage…`, `…mega.co.nz.evil.com`, `[::1]`,
  decimal/hex IPs and backslash tricks were all rejected.
* DNS pinning: `_public_address` resolves once and `HTTPSConnection` is
  monkey-patched to connect to that IP while keeping the hostname for Host/SNI/
  certificate verification (`:490-530`). Live TLS decrypt proves the pin works.
* Redirect refusal: `http.client` does not follow; `_validate_status` treats
  3xx as `502`. Only fixed headers (`User-Agent`, `Accept`, `Accept-Encoding:
  identity`, `Connection: close`, translated `Range`) are sent; no caller
  headers or cookies reach MEGA.
* Response headers: no upstream `Set-Cookie`/`Location`/`Content-Encoding`
  passthrough; `Content-Disposition` filename is control-char-stripped and
  RFC 5987-encoded. `Cache-Control: no-store`, `X-Content-Type-Options:
  nosniff`, `Cross-Origin-Resource-Policy: same-origin`,
  `Referrer-Policy: no-referrer` are present live.

### Range correctness (live NAS)

Against the 312,896-byte fixture, every request below returned the exact
plaintext slice (byte-for-byte against a full local decrypt):

| Request | Status | Content-Range | Content-Length | Match |
|---|---|---|---|---|
| `bytes=0-15` | 206 | `bytes 0-15/312896` | 16 | yes |
| `bytes=1-15` | 206 | `bytes 1-15/312896` | 15 | yes |
| `bytes=16-31` | 206 | `bytes 16-31/312896` | 16 | yes |
| `bytes=17-33` | 206 | `bytes 17-33/312896` | 17 | yes |
| `bytes=1001-1010` | 206 | `bytes 1001-1010/312896` | 10 | yes |
| `bytes=312000-312895` | 206 | `bytes 312000-312895/312896` | 896 | yes |
| `bytes=312000-` | 206 | `bytes 312000-312895/312896` | 896 | yes |
| `bytes=-1000` | 206 | `bytes 311896-312895/312896` | 1000 | yes |
| `bytes=4096-8191` | 206 | `bytes 4096-8191/312896` | 4096 | yes |
| no Range | 200 | — | 312896 | full SHA match |
| `bytes=312896-` | 416 | `bytes */312896` | JSON | correct |
| `bytes=10-5` / `bytes=abc` / multi-range | 416 | absent | JSON | correct status, CR minor |
| `bytes=-999999` | 206 | `bytes 0-312895/312896` | 312896 | yes |
| HEAD no Range | 200 | — | 312896 | no body |
| HEAD `bytes=100-199` | 206 | `bytes 100-199/312896` | 100 | no body |

Upstream request mapping was verified at the block boundary: `bytes=17-33`
produced an upstream `Range: bytes=16-33`; the 1-byte prefix was discarded.
Open-ended, suffix, mid-block, block-aligned, end-of-file and oversized ranges
all behaved correctly. `Accept-Ranges: bytes` is on 200 and 206. The upstream
ignores-Range fallback (200 → skip `start` bytes) is covered offline; live
MEGA honoured Range.

### Crypto

* `derive_content_key` (`mega.py:408-417`): AES-128 key =
  `key[0:16] XOR key[16:32]`; IV = `key[16:24] + 00×8`. Correct per the MEGA
  scheme.
* Counter: pycryptodome AES-CTR with 8-byte nonce and 64-bit big-endian
  `initial_value = block`; equivalent to MEGA’s 128-bit counter block starting
  at `nonce || counter`. `_new_cipher` (`mega_stream.py:849-861`) positions at
  `server_start // 16` and discards `server_start % 16` bytes.
* Independent check: the live 312,896-byte ciphertext decrypted with
  `openssl enc -aes-128-ctr -K <attribute key> -iv <content IV>` produced
  SHA-256 `998c100230edfb4e6637f65f818ed97c50555eb19df1944f1bc50dce7d4258c4`,
  identical to the proxy and to the local full-file decrypt. PNG is 3840×2160,
  8-bit RGB, non-interlaced.
* No plaintext/key logging: the stream logger only emits class/status/limit
  messages; the offline log test passes; live container logs contain no link
  key or URL.
* AES-CTR gives confidentiality, not integrity: there is no MAC/hash to verify,
  and MEGA storage URLs may be plain `http`. A network attacker between the NAS
  and MEGA could flip plaintext bits (media corruption), but cannot change the
  fixed `Content-Type`/`nosniff` response into script execution. This is a
  media-integrity limitation, not a code-execution path.
* Content-Type mapping from filename: `.mp3 → audio/mpeg`, `.m4a/.mp4 →
  audio/mp4`, `.wav/.flac/.ogg/.oga/.opus/.webm/.aac`; unknown → octet-stream.
  The PNG fixture correctly served `application/octet-stream`; audio mapping is
  unit-tested.

### DoS/resource basics

* 64 KiB chunks, `read1`, backpressure via generator; no accumulation; 8 GiB
  byte cap; 4 h wall deadline; 30 s read timeout; 10 s connect timeout.
* Client close/generator close releases the semaphore and closes the upstream
  (`_Once`, `finally`, `call_on_close`); offline slot count returned 4→4.
* `MAX_STREAMS` 4/process; 5th stream waits 2 s then `503 busy` with
  `Retry-After: 2`.
* Memory per stream is bounded (chunk + cipher); the resolver cache is TTL’d
  but unbounded in entry count (pre-existing; see residual risks).

### Deployment identity

* `/healthz` → 1.6.64; image labels 1.6.64/`6eb01db`; tag `v1.6.64`;
  `origin/main` = `6eb01db`; image ID and revision match. Traceable.
* Env: `SECRET_KEY` set, `RAVEWEB_ENV=production`, no
  `RAVEWEB_MEGA_TOKEN_SECRET`, no `RAVEWEB_MEGA_STREAMS` → derived key and
  4/worker. Gunicorn 2 workers × 8 threads.
* No reverse proxy on 8097; gunicorn access log off; `docker logs` shows no
  URL/key. If you add Caddy/nginx, configure query redaction before exposing
  stream URLs.

---

## Live checks performed

### Public PNG fixture (Kodi samples link)

`https://mega.nz/file/raByiTib#Fje3cxhPb4NkBJ936KVQjPNdSwslGqiboa8IYb05t5Q`

* Resolve: API 200, decrypted name
  `Quick Brown Fox 4K v3 (Use with 4-4-4 Chroma Subsampling).png`, size
  312,896, storage host `gfs208n127.userstorage.mega.co.nz` (later
  `gfs270n500.userstorage.mega.co.nz`).
* Full stream: 312,896 bytes, PNG 3840×2160, SHA-256
  `998c100230edfb4e6637f65f818ed97c50555eb19df1944f1bc50dce7d4258c4`.
* OpenSSL cross-check: identical SHA-256.
* NAS 1.6.64 token and cookie paths: HEAD 200/CL 312896, GET 200 full SHA match,
  Range 17-33 206 exact slice `000f000000087008020000001fdc5c2500`, 416
  `bytes */312896`, POST 405, tampered/expired/other-link 401.
* `/api/resolve` live with a session: 200, `source=mega`, `url` = raw storage
  host, `stream` present (163 chars), `expires` ~20 min, headers only
  `User-Agent`. `/api/mega/sign` live: 200, keys
  `absolute_url/expires/ok/path/source/url`, path 163 chars.
* `/api/mega/sign?u=<non-Mega>` → 400 `unsupported`; `/api/mega/sign` for an
  EBLOCKED link still mints a token (200); stream then 404 `not_found`.

### OSA audio links (all EBLOCKED)

Threads checked:

* <https://www.oldskoolanthems.com/threads/tony-humphries-ministry-of-sound-london-may-1993-double-pack.108127/>
  — two MEGA links (`4rh3jLKA`, `tn4QnYSD`)
* <https://www.oldskoolanthems.com/threads/dave-seaman-the-gold-collection-1995-single-tape.131780/>
  — `luZSyQrI`
* <https://www.oldskoolanthems.com/threads/angel-golden-1995.132451/> —
  `tg4yXISY`

All four returned `MEGA API error -16 (EBLOCKED)` (404 `not_found`) from the
Mac resolver and, for the one checked through the NAS, the same 404. No
non-EBLOCKED OSA audio fixture was available to test; the PNG fixture plus the
audio Content-Type unit tests and the AES-CTR cross-check cover the same code
path.

### Two-use storage claim

One fresh URL served 10/10 sequential `Range: bytes=0-0` requests with 206; the
documented “roughly two-use” limit did not reproduce. The streamer’s
cached-URL-then-403 recovery still worked in the offline tests and is harmless;
update the docs/expectations rather than relying on the limit for rotation.

### Tests

Focused suites:

```
.venv/bin/python -m pytest -q test/test_mega_stream.py \
  test/test_mega_resolver.py test/test_resolver_service.py
226 passed
```

I did not complete the full ~493-test suite in this pass (the focused modules
are the changed surface). The offline probes in this report were ad hoc fakes
and are not committed as tests; the fixes below should add them.

---

## Residual risks / out of scope

* The resolver cache (`mega._CACHE`) and the auth/session DB are pre-existing;
  the cache is TTL’d but not size-capped. An authenticated user resolving many
  distinct links can grow it slowly.
* The proxy is MEGA-file-only, but it will decrypt/serve **any** public MEGA
  file, not just audio (unknown extensions become `application/octet-stream`).
  Authenticated users can therefore use the NAS as a bounded MEGA relay. If
  that is not acceptable, add an extension/MIME policy or an operator allowlist
  for streamed MEGA links.
* The generic `/api/stream` proxy’s SSRF rules were audited in
  `RESOLVER-SECURITY-1.6.62.md`; this report only covers how the new Mega
  streams interact with its limits.
* Kodi/Shield were not altered or run. The Kodi-side statements are source
  review of `raveplayer/` and `raveplayer-worktrees/resolver-module/`, not a
  device test.
* Live checks used the public fixture and public OSA links; no owner media was
  downloaded, and no secrets/tokens are reproduced in this document.

---

## Recommended order of work

1. **Kodi client contract** (P1-1): prefer `stream`, handle session/bearer,
   parse expiry, add a Range test. This is the difference between “safe to
   expose” and “works on Kodi tonight”.
2. **Opaque signed URL** (P2-1/P2-5): stop putting the MEGA key in the URL;
   add per-token revocation and a shorter player TTL.
3. **Rate limiting** (P2-2): token/IP/link buckets; reuse the resolver limiter.
4. **206 hardening** (P2-3) and **cap composition/HEAD** (P2-4).
5. **`is_global` DNS check** (P2-6), Range RFC edges (P3-1), error mapping
   (P3-3), docs (P3-2), signing GET/Host/audit (P3-4), deployment hardening
   (P3-5).

**Bottom line:** the decrypting proxy is cryptographically correct and
fail-closed; I would expose it to authenticated LAN clients (and to a player
that is handed a signed URL) tonight. I would **not** advertise it as a Kodi
feature until P1-1 is fixed, and I would not put it behind a query-logging
proxy or share signed URLs outside the LAN.
