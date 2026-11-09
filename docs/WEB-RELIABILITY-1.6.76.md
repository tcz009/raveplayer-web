# Web reliability 1.6.76 — first-attempt login and mid-stream stall

Two live-field fixes on top of 1.6.75 (`e2476e4`): the intermittent
first-attempt login failure and 20bensons playback stopping after ~1 minute.
No deploy or push was performed for this branch.

## BUG 1 — login fails once, then succeeds

### Evidence

The live audit store (`/data/auth.sqlite3`, read-only) on `thinkomv`
(`192.168.1.179:8097`, revision `e2476e4`) showed, for the owner's Safari
client at `192.168.1.167`:

| time (UTC) | event | identity | result |
|---|---|---|---|
| 12:19:13 | `login_failure` | `admin` | 401 |
| 12:19:23 | `login_success` | `owner` | 302 |
| 12:53:06 | `login_success` | `owner` | 302 |
| 12:53:07 | `login_csrf_rejected` | — | 400 |
| 12:55:44 | `login_success` | `owner` | 302 |
| 12:55:45 | `login_csrf_rejected` | — | 400 |
| 13:00:32 | `login_success` | `owner` | 302 |
| 13:00:33 | `login_csrf_rejected` | — | 400 |

The whole day had 317 `login_success`, **3** `login_csrf_rejected` and exactly
**1** `login_failure` (`admin`).  The limiter row was
`192.168.1.167\0admin`, failures=1.

Two distinct failure modes are proven:

1. **Duplicate POST after a successful login.**  Every `login_csrf_rejected`
   lands 0.4–1.1 s *after* a `login_success`, with the app's `/` and API
   requests already authenticated in between.  The first POST rotates the
   session and its CSRF token; the second POST (double-click, second Enter,
   restored/duplicated tab) still carries the pre-rotation form token, so it
   failed CSRF and its 400 page ("Your sign-in session expired") could replace
   the successful navigation.  The audit row explains why the owner saw only
   successful logins around the reports.
2. **Legacy `admin` username.**  The owner's password manager filled `admin`
   (the old alias) once; the password was right but the identity was unknown.
   The session/table then retried as `owner` ten seconds later.

A third component is the **service worker on HTTPS/secure origins** (Tailscale,
a reverse proxy, or a home-screen PWA).  Its navigation handler intercepted
`/login` and re-fetched it with `fetch()` inside `respondWith()`.  Chrome does
not commit `Set-Cookie` from a response returned that way, so the login page
rendered a CSRF token while the browser jar stayed empty; the first POST then
failed with `login_csrf_rejected reason=missing_session`.  The 400 page is a
POST response (not intercepted), so retrying or reloading usually worked —
exactly "fails once, then succeeds".  This is reproducible with an active
service worker (`test_e2e/test_login_reliability.py`: the stale-session test
asserts the cookie is stored while the worker is controlling the page).

A fourth, client-side component: the owner's sessions are `remember=0`
(browser-session cookie).  A valid owner session at 17:43:36 was used until
17:45:08, but `GET /` at 17:45:22 was unauthenticated while the server-side
session row was still present and not revoked — i.e. Safari discarded the
session cookie (window/app close), not a server expiry.  The stale-cookie path
is therefore hardened and regression-tested as well.

### Fixes

- **Idempotent duplicate POST**: if `_session_valid()` is already true,
  `POST /login` redirects to the validated `next`/`/` *before* the CSRF check.
  The duplicate can no longer paint a 400 over a good login.  Login redirects
  now carry `Cache-Control: no-store`.
- **Owner alias**: `admin` maps to the canonical `owner` identity (same hash,
  same `admin` role, same limiter key, no second account).  Audit detail records
  `alias=admin`; a wrong password still returns the generic 401.  Documented in
  `docs/AUTH.md`.
- **Reliable CSRF auditing**: `login_csrf_rejected` now records the typed
  identity and `reason=missing_session|missing|mismatch`, and re-renders with
  the session's current token so the next submit succeeds.  Rejections never
  touch the failed-login counter.
- **Stale cookie hygiene**: `GET /login` clears partial/stale auth keys from
  the session before rendering (fresh CSRF-only cookie), while a plain
  anonymous cookie keeps its token so two tabs share one form.
- **Audit-store resilience**: login audit writes, limiter reset and limiter
  failure recording are best-effort; a busy SQLite store can no longer turn a
  correct login into a 500.  A failed `create_session` returns a retryable 503.
- **Client**: `static/login.js` makes the form single-shot (button disabled,
  repeated `submit` prevented) and reloads a bfcache-restored login page so its
  CSRF token matches the current cookie.
- **Service worker**: `/login` and `/logout` navigations are no longer
  intercepted at all; the browser fetches them directly so their `Set-Cookie`
  is applied.  Other deep links keep the existing network-first shell fallback.  Only a direct 200 at `/` is cached as the app shell.
  A signed-out `/` fetch follows the 302 to `/login`; that HTML is no longer
  cached under `/` (which used to make the first post-login navigation show
  the login page again).  `CACHE_NAME` bumped so existing poisoned caches are
  purged on activation.

### Regression tests

`test/test_auth.py` (+6):

- first fresh-page POST with correct credentials → 302 and owner session;
- duplicate POST after success → 302, no `login_csrf_rejected`;
- stale server-side session cookie → fresh CSRF, login succeeds, no
  `login_failure` and no limiter count;
- stale CSRF → 400 `reason=mismatch`, no failure count, immediate retry works;
- `admin` alias → owner, audit `alias=admin`, limiter keyed on `owner`;
- broken audit store → login still succeeds;
- GET /login preserves anonymous CSRF but rotates a stale auth cookie.

`test_e2e/test_login_reliability.py` (+3, real Chrome): fresh context first
POST succeeds and repeats twice after logout; stale session cookie shows a
fresh form, stores the new cookie while a service worker is active, and logs in
without a failure event; a browser `fetch` duplicate of the login POST follows
to the app instead of showing the CSRF error.

## BUG 2 — 20bensons playback stops after ~1 minute

`/api/stream` proxies MP3s with `READ_TIMEOUT = 30`.  A mid-body upstream
stall raised `socket.timeout` in `generate()`, which closed the response.  The
browser's `<audio>` element saw a truncated (chunked) body and stopped, e.g.
JUMPING JACK FROST at 57 s or GROOVERIDER/PILGRIM at 83 s.

### Fix: bounded resume-on-stall

- After **any** read failure (`socket.timeout`, `http.client.HTTPException`,
  `OSError`, `ValueError`) with `sent > 0`, the proxy reopens the **same final
  URL** with `Range: bytes=<start+sent>-<end>`.
- Resumable only when the original response proved Range support
  (`Accept-Ranges: bytes` or a parsed 206 `Content-Range`).  The original range
  start/end are kept, so a client `Range: bytes=100-109` resumes at 103–109 and
  never grows past 109.
- A resume is accepted only for a **206 whose `Content-Range` starts at
  exactly the requested offset**; 200 (Range ignored), 416, a shifted range,
  non-media or encoded bodies are rejected and the response ends as before.
  Therefore bytes are never duplicated or skipped.
- `If-Range` is sent with the original `ETag`/`Last-Modified`, so a changed
  file answers 200 and is rejected instead of splicing two entities.
- At most `MAX_STREAM_RESUMES = 3` reopens per request; the absolute 4 h
  deadline, 8 GiB cap and per-request stream slot are unchanged.  Slot release
  still happens exactly once in `generate()`'s `finally`.
- The response remains chunked and `Content-Length` is still not forwarded:
  only the bytes actually sent are promised, so a later stop is visible to the
  client instead of waiting for a stale length.  206 responses keep their
  original `Content-Range`.
- Normal (no-stall) requests are untouched: no extra connection is opened and
  a non-Range source still truncates exactly as before.

### Tests

`test/test_stream.py` (+8): 200+`Accept-Ranges` stall resumes with
`bytes=6-`; OSError resume; no resume without Range support; client-range
resume clipped at the original end; shifted `Content-Range` refused; ignored
Range (200) refused; resume count capped; resumed requests release the stream
slot; resumed body carries no `Content-Length`.

## Verification

```
RAVEWEB_CHROMIUM_EXECUTABLE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  pytest -q test          # 978 passed (twice, final tree)
RAVEWEB_CHROMIUM_EXECUTABLE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  RAVEWEB_REQUIRE_BROWSER=1 pytest test_e2e -q   # 43 passed (twice)
```

Targeted runs: `pytest -q test/test_stream.py test/test_auth.py` (93 passed) and
`pytest test_e2e/test_login_reliability.py` (3 passed).

One unrelated test flake was fixed while validating the double run:
`test_mega_stream.py::test_route_rejects_tampered_and_expired_tokens` changed
the last base64 character of the HMAC token, which can alter only padding bits,
so the decoded signature sometimes stayed valid.  It now tampers a middle
character.  (An intermediate full run also hit the pre-existing intermittent
`test_live_radio_pages_at_40` KeyError once; it passed on every rerun and both
final full runs.)
