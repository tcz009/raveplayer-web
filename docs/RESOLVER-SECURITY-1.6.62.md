# Rave Resolver — security & privacy audit (web 1.6.62)

**Status:** report only. No source, config, deploy or Kodi/Shield changes were
made; this document is the only file added. **Audited target:** tag
`v1.6.62` = `aebd8af2433807edfb7f953327c34c32e8fef755`
(“1.6.62: keep Mega fragment key when resolving”), which is what the NAS
reports live (`GET /healthz` → `{"version":"1.6.62"}`). **Live auth check:**
`GET /api/resolve` without a session → `401`.

> **Scope/provenance correction.** At the start of this audit local `main` was
> `de39021` (**1.6.59**) and `v1.6.62` existed only on the local
> `feat/resolver-integration` branch, not on `origin/main`. During the audit
> `origin/main` advanced to `aef9ace` (**1.6.63**, 1.6.62 plus the new
> `core/resolver/mega_stream.py`). Every line number below is from **tag
> `v1.6.62`** (`git show v1.6.62:<path>`), not from the moving worktree.
> 1.6.63 is **not** covered (see “New 1.6.63 surface” at the end).

**Scope:** `core/resolver/{base,service,cache,direct,podomatic,mediafire,drive,mega}.py`,
`__init__.py`, app registration, `requirements.txt`, and the existing
`core/fetch.py` guards where the resolver depends on them. The Mediafire/Drive
adapters delegate to `core/ezgarage.py` / `core/drive.py`; those files are
included where the delegation changes the security result. Kodi, the Shield,
`/api/stream` internals and 1.6.63's `mega_stream.py` are out of scope.

**Method:** source review of the tag, offline probe scripts (no network, no
file writes), the resolver test subset (`191 passed`), and live read-only
`/healthz` + unauthenticated `/api/resolve` checks.

---

## Verdict

**P0 — none found.** `/api/resolve` is behind the same authenticated `/api/`
guard as every other API (live 401), the host/private/redirect/header guards
work, and the service does not proxy media. **One P1** (worker exhaustion / a
latent redirect-SSRF path through the two legacy adapters) and **eight P2**
items.

| # | Sev | Finding | 1.6.63 status |
|---|---|---|---|
| P1-1 | **P1** | Mediafire/Drive adapters bypass the bounded, redirect-free resolver client; 25 s hard-coded timeout, no wall-clock read deadline, process-global lock | unchanged |
| P2-1 | P2 | Unbounded provider caches + limiter/host-lock maps; per-subdomain buckets multiply the provider budget | unchanged |
| P2-2 | P2 | Result-host allowlist is optional; Drive can return any public host | unchanged |
| P2-3 | P2 | Mega typed failures are not translated: 404→502, 429 loses `Retry-After`, tracebacks logged | **fixed** on 1.6.63 |
| P2-4 | P2 | Mega fragment / signed query retained raw in cache keys; `MegaLink` repr prints the key; docs steer clients to GET | unchanged |
| P2-5 | P2 | `_authorized()` fallback accepts `auth`+`sid` without identity/role/expiry/revocation | unchanged |
| P2-6 | P2 | `pycryptodome>=3.20,<5` is a floating range; Docker build not reproducible | unchanged |
| P2-7 | P2 | Non-default ports accepted on returned URLs | unchanged |
| P2-8 | P2 | Deployed 1.6.62 was not traceable to the stated `main` at audit start; main then moved to 1.6.63 | n/a |

---

## P1 — worker exhaustion / latent redirect SSRF (Mediafire & Drive)

**Where**

* `core/resolver/mediafire.py:29-31` stores `timeout/http/dns` and then never
  uses them; `:37-43` calls `core.ezgarage.resolve_page(page)`.
* `core/resolver/drive.py:25-27` does the same; `:35-40` calls
  `core.drive.resolve(url)`.
* `core/ezgarage.py:44` `MIN_GAP = 1.2`, `:47` `TIMEOUT = 25`, `:116`
  process-global `_LOCK`, `:229-266` `_fetch_page()` uses
  `urllib.request.urlopen(...)` (`:244`) and a **single**
  `response.read(MAX_PAGE_BYTES + 1)` (`:258`); `:322-373` holds `_LOCK`
  across the politeness sleep and the fetch.
* `core/drive.py:40` `TIMEOUT = 25`, `:44` process-global `_LOCK`, `:162-196`
  `_probe()` uses `urllib.request.urlopen(...)` (`:175`) and
  `response.read(4096)` (`:190`); `:215-234` may run two probes; `:247` holds
  `_LOCK` for all Drive resolutions.
* `core/resolver/service.py:256-263` builds and passes a
  `base.UrllibHTTPClient(timeout=timeout, dns=dns)`; the two adapters ignore
  it. The resolver's documented `RESOLVER_TIMEOUT` (clamped 0.5–15 s) does not
  apply.

**Why it matters**

1. **Timeout bypass / slow-drip DoS.** The legacy helpers use a per-recv
   socket timeout, not a wall-clock budget. A server that sends one byte just
   under the timeout keeps a worker in `response.read()` far past 25 s. This
   is exactly the class fixed in `core/fetch.py` (W2) but the resolver's two
   legacy paths never went through it.
2. **All requests serialise.** `ezgarage._LOCK` and `drive._LOCK` are
   process-global. Under the container's 8 gunicorn threads (`Dockerfile:28`),
   eight concurrent Mediafire requests for *different* URLs queue behind one
   lock: each miss sleeps 1.2 s and may fetch for 25 s, so ~8 × 26 s pins every
   thread in that worker; two workers → the whole API is unavailable. A
   friends-level account is enough (the app is LAN-only but friends are not
   fully trusted).
3. **Redirects are followed, not refused.** `urlopen()` follows 3xx with
   urllib's default handler and no private-address revalidation. The initial
   host is constrained to `*.mediafire.com` / Google, so this is **latent**,
   not a directly attacker-chosen SSRF; a provider-side redirect (or a future
   Mediafire open redirect) to `http://169.254.169.254/…` or the LAN would be
   followed and its body parsed, contrary to `docs/RESOLVER-SERVICE.md:147-149`.
   The same two helpers also honour `HTTP(S)_PROXY` from the environment,
   unlike the resolver's own client and Mega.

**Proof (run offline against the tag, no network)**

```text
MediafireResolver(timeout=0.1, http=object(), dns=...)  -> timeout=0.1, http attr=False, dns attr=False
DriveResolver(timeout=0.1, http=object(), dns=...)       -> timeout=0.1, http attr=False, dns attr=False
ezgarage.TIMEOUT=25  drive.TIMEOUT=25
mediafire resolve with timeout=0.1 and a 0.6 s fake page -> 0.61 s
drive resolve     with timeout=0.1 and a 0.6 s fake probe -> 0.61 s
drip page: timeout=0.3 s, 10 bytes at 5 B/s              -> _fetch_page took 1.85 s
local 302 -> http://127.0.0.1:<port>/secret              -> _fetch_page returned "INTERNAL-SECRET"
base.UrllibHTTPClient on the same 302                    -> refuses (BlockedURL before the request)
```

At 4 MiB and a 25 s per-recv timeout the drip case is effectively unbounded.
`test/test_resolver_mediafire_drive.py` monkeypatches `resolve_page` /
`drive.resolve`, so it never exercises these rails.

**Fix**

* Use `self.http.request(...)` with `self.timeout` and `self.dns` in both
  adapters (or teach `core.ezgarage._fetch_page` / `core.drive._probe` to take
  the resolver's `UrllibHTTPClient`).
* Add a wall-clock deadline and the 4 MiB / 4 KiB caps around the reads
  (reuse the `core/fetch._read_limited` pattern), and clamp to
  `base.MAX_TIMEOUT` (15 s).
* Refuse redirects for the Mediafire page fetch; for Drive, validate every hop
  against the Drive allowlist and re-check private addresses.
* Drop the second global lock (the service already has per-host
  single-flight) and add a resolver-wide semaphore plus a per-identity quota.
* Add offline tests with a drip server and a 302-to-loopback server.

---

## P2-1 — unbounded caches and limiter state; subdomains multiply budgets

**Where:** `core/resolver/mega.py:762` (`_CACHE = {}`), `:806-825`
(`_cache_put_value` / `_cache_put_error` never trim); `core/ezgarage.py:117`
(`_CACHE`), `:171-185`; `core/drive.py:45` (`_CACHE`), `:154-159`;
`core/resolver/service.py:61-107` (`TokenBucket._state` per exact host);
`core/resolver/cache.py:72` (`_host_locks`), `:186-193`.

**Proof**

```text
rate=1/min, burst=1, 20 distinct https://nN.mediafire.com/a.mp3 URLs
  -> accepted=20, limited=0, bucket states=20
same host twice -> second request 429 (retry_after=60)
```

The service cache is capped at 2048 entries (`cache.py:64-68`), but the three
provider caches, `TokenBucket._state` and `_host_locks` have no cap or sweeper.
Any authenticated user can add entries with random Mediafire/Drive ids or
`*.mediafire.com` / `*.google.com` subdomains; entries persist for the worker's
life (the TTL is only checked when the *same* key is looked up). Each new
subdomain also gets a fresh burst, so the per-host limit does not bound
provider work. `mediafire.py:33-35` and `drive.py:29-33` deliberately match
arbitrary subdomains.

**Fix:** bounded LRU + expired-entry sweep for every provider cache; cap
`_host_locks` and `_state` (or key buckets by registrable domain/provider);
add a per-identity/IP request quota at `/api/resolve`; keep the service cache
cap.

## P2-2 — result-host allowlist is optional; Drive can return any public host

`service.py:171-185` only enforces `adapter.allowlist` when the attribute
exists. Only `direct` and `podomatic` set it. `drive.py:35-49` has no
allowlist, and `core.drive._resolve_unlocked` returns `response.geturl()`
(`core/drive.py:224/234`) without a host check. The repository's own test
expects `https://cdn.example.com/audio.mp3` (`test/test_resolver_mediafire_drive.py`).

```text
DriveResolver.resolve -> https://cdn.example.com/audio.mp3
service accepted: host=cdn.example.com, adapter has allowlist=False
```

That is public-only, not private, but it violates the documented “provider's
direct-file host” rule and means a provider bug/redirect can turn the resolver
into an echo of arbitrary public URLs. **Fix:** make `allowlist` mandatory on
the `Resolver` protocol, fail 502 when absent, and add:
Drive `drive.usercontent.google.com` (+ its documented storage suffix),
Mediafire `download*.mediafire.com`, Mega `*.userstorage.mega.{co.nz,nz,io}`.

## P2-3 — Mega errors are not translated into the service contract

`core/resolver/mega.py:1009-1034`: `MegaResolver.resolve()` calls
`mega.resolve()` with no `except MegaError`. `mega.MegaError` is **not** a
subclass of `base.ResolverError` (`mega.py:141-143` aliases it only inside
that module). Therefore `NotFound`/`RateLimited`/`Unsupported` reach
`service.py:333-337` as “unexpected”, producing:

```text
HTTP 502 {"code":"upstream","error":"resolver failed","ok":false}
+ logger.exception("resolver: unexpected failure")  (full traceback)
```

A dead MEGA link should be 404, and a MEGA 429 should be 429 with
`Retry-After`; as written, clients retry a 502 and can hammer MEGA, and the
negative cache in `service`/`cache.py` never sees a typed error. **Fix:** catch
`MegaError` in `MegaResolver.resolve` and map `Unsupported→base.UnsupportedMedia`
(or `UnknownURL`), `NotFound→base.NotFound`, `RateLimited→base.RateLimited`
(with `retry_after`), `FormatChanged/UpstreamError→base.UpstreamError`
(deadline→`base.UpstreamTimeout`). This is already fixed on `origin/main`
1.6.63 via `_map_error`; keep it when deploying.

## P2-4 — secret-ish material in cache keys and `MegaLink` repr; GET docs

`service.py:156` keys the service cache with the **raw** URL:
`"<source>:<raw_url>"`. For MEGA that includes the fragment:
`mega:https://mega.nz/file/abcdefgh#<32-byte-key>`; `mega.py:227-228` keys its
own cache `mega:<handle>:<key>` (`_CACHE` at `:762`). The fragment is the
file decryption key; a core dump, debugger, or any future cache-dump/debug
endpoint exposes it. `MegaLink` (`mega.py:211-231`) marks only `key_bytes` as
`repr=False`; `repr()` and `str()` print the key:

```text
MegaLink(handle='abcdefgh', key='a2tra2...s')
https://mega.nz/file/abcdefgh#a2tra2...s
```

`docs/RESOLVER-SERVICE.md:29-40` advertises `GET /api/resolve?u=...`. A real
browser never sends a fragment in a GET, so MEGA links silently lose their
key; if a client percent-encodes `#` as `%23`, the key lands in query strings,
reverse-proxy logs, browser history and `Referer`. (The Flask access log and
the resolver's own logs do **not** log query strings — see “Privacy” — but
intermediaries may.) The same applies to signed Mediafire/direct query strings.

**Fix:** key caches by `HMAC(server_secret, fragment/query)` (or
`sha256(fragment)[:16]`), never the raw secret; set `field(repr=False)` on
`MegaLink.key` and add a `__repr__`/`__str__` that prints the handle/path only;
make MEGA POST-only (or accept a separate `key` field over POST) and update the
docs to prefer POST for all provider links.

## P2-5 — auth fallback is weaker than `_session_valid`

`core/resolver/service.py:285-306`. If `import app` fails (or the blueprint is
mounted standalone), the last branch returns
`bool(session.get("auth") and session.get("sid"))`. It skips identity/role
checks, the server-side `audit.session_check` (revocation/block), and the
session age check that `app.py:776-815` performs. The cookie is signed, so this
is not session forgery, but a revoked or expired cookie is accepted while the
fallback is active. Demonstrated offline:

```text
monkeypatched import failure + session {auth, sid} only
-> GET /api/resolve 200 (no identity/role, no audit lookup)
```

**Fix:** remove the fallback; require `RESOLVER_AUTH_CHECK` (callable) or fail
closed; never treat the mere presence of `auth`/`sid` as authentication.

## P2-6 — `pycryptodome` is a floating range; build not reproducible

`requirements.txt:8` is `pycryptodome>=3.20,<5`, while every other runtime
dependency is exact-pinned (`Flask==3.0.3`, `gunicorn==22.0.0`,
`argon2-cffi==23.1.0`, `bcrypt==4.2.0`). `Dockerfile:10-11` runs
`pip install --no-cache-dir -r requirements.txt` with no hashes/lock, so two
builds can ship different crypto libraries and a future 4.x/5.x is adopted
without review. The local 1.6.62 venv contains pycryptodome **4.4.0**;
`docs/MEGA-RESOLVER.md:333-335` still says the dependency is “not in
`requirements.txt`” (stale).

**Known-vulnerability check.** The only use is
`Crypto.Cipher.AES.new(key, AES.MODE_CBC, iv).decrypt(...)`
(`mega.py:393-404`); no RSA/OAEP, no signing. CVE-2023-52323 (OAEP/Manger
side-channel, fixed in 3.19.1) is excluded by the `>=3.20` floor; the
3.24.1 security fix (ECDSA/EdDSA/DSA signing timing, Oct 2026) does not apply
to AES-CBC. So there is **no known vulnerable use in this code path**, but the
range admits 3.20.0–3.24.0 (affected for other algorithms) and future majors.
See the [PyCryptodome changelog](https://www.pycryptodome.org/src/changelog)
for 3.19.1 / 3.24.1.

**Fix:** pin `pycryptodome==4.4.0` (the version in the built venv) or a
reviewed 4.4.x, add a constraints/hash lock and `pip-audit` in CI, and correct
`docs/MEGA-RESOLVER.md`.

## P2-7 — non-default ports accepted on returned URLs

`base.py:322-327` allows any port 1–65535; `normalize_url` preserves it
(`:351-354`); `validate_resolved` does not restrict it. `direct.py:85-101`
therefore returns e.g. `https://archive.org:22/x.mp3` if the host/path match,
and `mega.py:726-731` accepts any port on an allowlisted MEGA storage host.
The server does not connect (Mediafire/Drive construct their own URLs), so
this is client-side protocol/port confusion rather than server SSRF, but it
contradicts the “port restrictions” part of the threat model. **Fix:** restrict
to 80/443 (or an explicit per-adapter port allowlist) in `normalize_url` /
`validate_resolved`.

## P2-8 — deployed 1.6.62 was not traceable to the stated `main`

At audit start: local `main` = `de39021` (`VERSION` 1.6.59,
`core/resolver/` absent); `v1.6.62` = `aebd8af` on local
`feat/resolver-integration`, not on `origin`; the NAS reported 1.6.62. During
the audit `origin/main` moved to `aef9ace` (1.6.63), which does contain
`aebd8af` as an ancestor and adds `mega_stream.py`. Until that merge, an audit
of `main` (as requested) would have missed the entire deployed resolver, a
routine deploy from stale `main` would have removed it, and there was no
immutable record tying the running image to a commit. **Fix:** merge/tag/push
the release, deploy from the tag (or record the image digest and OCI
version/revision labels), and make the release checklist verify
`/healthz` plus the image digest against the tag.

---

## Confirmed controls (what is solid)

* **Host allowlists are exact/suffix only.** `base.HostAllowlist` rejects
  wildcards and path/userinfo characters; `allows()` is
  `host == allowed or host.endswith("." + allowed)` (`base.py:389-436`).
  Probes: `notexample.com`, `example.com.evil.com`, `evil-example.com` all
  false; `*.example.com` and bare `*` raise.
* **Private/loopback/link-local/metadata/localhost are rejected before any
  adapter work, with no DNS for the cheap check** (`base.py:456-505`), then
  every DNS answer is validated (`check_public_host`, `:533-556`). Covered:
  `127.0.0.1`, `localhost`, `[::1]`, `169.254.169.254`, `10/8`, ULA,
  `fe80::`, IPv4-mapped IPv6, decimal (`2130706433`) and hex (`0x7f000001`),
  mixed public/private DNS, `.local`/`.internal`/`.home.arpa`, single-label
  Docker/K8s names. `RAVEWEB_ALLOW_PRIVATE=1` is deliberately ignored by the
  resolver (`base.py:62-76,456-478`). `127.1`/`0x7f.1` are not caught by the
  no-DNS pre-check but are rejected by `check_public_host` (they resolve
  127.0.0.1) and no shipped adapter matches them.
* **Redirects are refused** by `UrllibHTTPClient` (`base.py:636-641,656-689`)
  and by Mega's API opener (`mega.py:482-496`), and the resolved URL is
  independently re-normalised and re-checked (`service.py:171-185`). The P1
  legacy paths are the exception.
* **Header sanitising** (`base.py:577-604`) rejects `Authorization`, `Cookie`,
  `Set-Cookie`, `Host`, `Content-Length`, `Transfer-Encoding`, hop-by-hop and
  forwarding headers, invalid names, CR/LF/NUL, and oversized sets; both
  request and response headers go through it.
* **Auth:** `/api/resolve` is covered by the app-wide `/api/` guard
  (`app.py:882-893`), with the blueprint repeating a check
  (`service.py:315-318`); live unauthenticated request → **401**. Any
  authenticated owner/friends viewer may resolve, which matches the rest of
  the LAN player. GET has no side effects; POST only fills caches/limiter, so
  CSRF is not a state-change issue (SameSite=Lax; `MAX_CONTENT_LENGTH` 64 KiB).
* **No secrets in normal server logs:** the app logs method/path/status/ms and
  request id only (`app.py:3811-3831`); the audit store records
  endpoint/bytes/identity/IP/UA/status, not the URL (`app.py:1335-1356`);
  gunicorn is started without an access log (`Dockerfile:28-30`). Error bodies
  are generic (`{"ok":false,"error":"resolver failed","code":"upstream"}`) and
  adapters' messages contain no submitted URL, signed query or token. There is
  no resolver disk cache.
* **Bounded work in the supported paths:** URL cap 2048, request body 8 KiB
  (global 64 KiB), timeout clamp 0.5–15 s (`base.py:38-41,261-269`), response
  caps 1 MiB (`UrllibHTTPClient`), 64 KiB (Mega), 4 MiB (Mediafire page),
  4 KiB (Drive probe); per-host single-flight; service cache capped 2048;
  Mega has a 15 s deadline, 3 attempts, gzip/response caps, no env proxy and
  no redirects.
* **Dependency import fails closed:** if pycryptodome is absent, `mega.py:49-52`
  sets `AES=None`, and `_aes_cbc_decrypt` raises a typed `MegaError` (code 500)
  → service 502; it never falls back to plaintext or another cipher.

---

## Owner’s no-popup / no-warning rules

**Unaffected, and no new UI was added by this release.**

* `static/app.js` and `templates/` contain **no** reference to
  `/api/resolve` (grep returns nothing); the SPA's `resolveAndPlay` targets
  `/api/dnb`, `/api/moxser`, `/api/mikus/drive` and similar, not this feature.
  `git diff v1.6.59..v1.6.62 -- static templates` changes only the service
  worker cache name (`static/sw.js`).
* The resolver is server-side JSON only: no `alert()`, `confirm()`, modal,
  banner, toast, warning page, focus change or new navigation. Errors are
  JSON bodies for API clients; the SPA never asks the resolver for a URL, so
  none of its error paths can surface in the player.
* The existing silent-failure rules stay exactly as documented:
  `docs/HARDENING.md:19-23` (RTP 401/403/timeout: **no banner/pop-up**),
  `docs/STREAM-SOURCE-BLOCKED.md:27-32` (**no banner, toast or “Check again”**),
  `docs/CODE-REVIEW.md:192` (**silent like Kodi**). The resolver does not touch
  `/api/stream`, RTP retry logic or the player state machine.
* If a future client is wired to `/api/resolve`, keep the same rule: do not add
  a popup/consent/warning; a failed resolve should be a quiet status line or
  nothing, consistent with the silent-source policy. The resolver docs' “use
  `code` for the user-facing message” line should be read as “use it for an
  existing quiet status”, not a new dialog.

---

## Privacy / data handling

* **Server logs:** no query string, no resolved URL, no token, no MEGA key
  (`app.py:3811-3831`). Request id only. The audit DB records that
  `/api/resolve` was called, by which identity/role/IP/UA, with byte count and
  status — not what was resolved (`app.py:1335-1356`). No analytics/tracking
  code exists in the resolver or the app.
* **Memory:** signed Mediafire URLs, MEGA storage capability URLs and the MEGA
  fragment are retained in process caches for the TTLs (5 min service / 2 h
  provider / 20 min MEGA); no disk persistence. See P2-4 for key hygiene.
* **No media proxying:** the resolver only returns URLs (and optional HEAD
  probes); it never streams audio bytes, so it cannot be used to exfiltrate
  content or bypass client egress rules. Mediafire fetches one HTML page
  (capped 4 MiB); Drive probes ≤4 KiB; Mega fetches one JSON API response
  (capped 64 KiB).
* **Client-visible capability URLs:** the signed Mediafire URL and MEGA
  storage URL are bearer capabilities returned in the JSON. Clients should not
  log them; the server does not.
* **GET caveat:** a reverse proxy in front of the NAS may log query strings;
  prefer POST for `/api/resolve` (especially MEGA, whose fragment cannot be
  sent as a real URL fragment in a GET).

---

## New 1.6.63 surface (out of scope — audit before deploy)

While this report was being written, `origin/main` became `aef9ace`
(“1.6.63: Mega decrypting stream proxy on resolver 1.6.62 base”). It adds
`core/resolver/mega_stream.py` (1047 lines) and an `app.py` auth exception for
`/api/mega/stream` backed by an HMAC token, plus `mega.py` name/size metadata.
That is a **new token-authenticated, decrypting, Range-serving network
surface** and is not covered by the checks above. `mediafire.py` / `drive.py`
are unchanged in 1.6.63, so P1-1 and P2-1/2/4/5/6/7 still apply there; P2-3 is
fixed by `_map_error`. The local worktree also had an uncommitted
`core/resolver/mega.py` edit during the audit; this report deliberately pins to
`v1.6.62`.

---

## Reproduction

```sh
cd "Rave Player"

# exact audited tree
git show -s --format='%H %d %s' v1.6.62
git show v1.6.62:core/resolver/mediafire.py | nl -ba
git show v1.6.62:core/ezgarage.py | nl -ba | sed -n '229,266p'

# live, read-only
curl -sS http://192.168.1.179:8097/healthz
curl -sS -o /dev/null -w '%{http_code}\n' http://192.168.1.179:8097/api/resolve   # 401

# offline resolver suite (no DNS/network)
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m pytest -q -p no:cacheprovider \
  test/test_resolver_service.py test/test_mega_resolver.py \
  test/test_resolver_mediafire_drive.py        # 191 passed

# P1 proofs: the adapter ignores timeout/http/dns; legacy helpers use 25 s and
# follow redirects; a 0.3 s timeout still reads a 1.85 s drip. See the code
# excerpts in P1-1.
```

No Kodi/Shield access was used, and no deploy or source change was made.
