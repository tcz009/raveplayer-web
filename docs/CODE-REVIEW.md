# Rave Player web — code review

Review of `raveplayer-web` (Flask app behind `http://192.0.2.10:8097`).
Scope: `app.py`, templates, CSS/JS, scripts, packaging, tests. The shared
provider modules in `core/` (except the Kodi shim) were off limits; findings
there are reported at the end instead of edited.

Everything below was run locally on the stated commit with the app served by
gunicorn on `127.0.0.1:8098` against the real Internet Archive, radio-browser,
RSS feeds and ravetapepacks.com, plus an offline pytest suite.

## TL;DR

The app was reachable and the Archive proxy worked, but three user-facing
things were broken (all of Legacy Sites, removing stream favourites, any
favourites store with one malformed entry), the proxy followed redirects into
private addresses, header injection produced a 500-class error that leaked the
internal exception, and the UI silently kept showing stale content when a
section failed to load. All of those are fixed, along with the worker
exhaustion risk and a long list of smaller correctness/UX issues.

- 153 offline tests added (was 0).
- No new runtime dependencies (pytest is test-only).
- JSON field names and `/api/stream` parameters unchanged.

## Genuinely broken (verified before/after)

### 1. Every Legacy Sites track failed to play (502)

`core/legacy.playable()` produces Kodi's header syntax
`https://…/track.mp3|Referer=…&User-Agent=…` and puts it in the `play` field.
The browser encoded that whole string as `u=`, and the proxy requested the URL
*including* `%7CReferer=…`, so ravetapepacks.com answered 404.

```
before: GET /api/stream?u=https%3A%2F%2Fwww.ravetapepacks.com%2F…%7CReferer%3D…
        → 502 {"error":"upstream 404"}
after:  → 206 audio/mpeg, 1001 bytes, Content-Range: bytes 0-1000/57268224
```

The same suffix was also stored in favourites, so favourited legacy tracks
were unplayable too. Fix: `/api/stream` splits the Kodi suffix into real
`Referer`/`User-Agent` headers (`_split_kodi_target`) before validating the
URL. This keeps the provider contract intact and works for external callers.

### 2. Stream favourites could not be removed (404)

The key is `stream:https://…`, i.e. it contains slashes, but the route was
`/api/favourites/<key>`. Werkzeug decodes `%2F` before routing, so
`DELETE /api/favourites/stream:https%3A%2F%2F…` never matched.

```
before: DELETE …/api/favourites/stream:https%3A%2F%2Farchive.org%2F…  → 404
after:  → 200 {"removed": true}; second call → {"removed": false}
```

Fix: route is now `<path:key>`; pack keys (no slash) still work.

### 3. `GET /api/favourites` returned 500 HTML on a partial entry

`api_fav_list` indexed `entry["target"]` directly. One hand-edited or
truncated entry (e.g. `{"title": "x"}`) took the whole list down.

```
before: 500 text/html (Internal Server Error)
after:  200 {"title":"Favourites","items":[]}
```

`POST` with a non-string title also returned 500 HTML; it is now
`400 {"saved": false, "error": "title is required"}`. Every favourites path is
also wrapped so a read-only or corrupt store can never 500.

### 4. SSRF guard was bypassable via redirects, and racy

- `urllib.request.urlopen()` follows redirects automatically, so a public URL
  that answers `302 http://127.0.0.1:8097/healthz` was fetched without any
  further check.
- `_host_allowed()` resolved the name, then `urlopen()` resolved it again: a
  DNS server could answer public for the check and private for the connection
  (DNS rebinding).
- `ALLOW_PRIVATE=1` also disabled the scheme check.
- No port rules, no userinfo rejection, no IPv4-mapped/`100.64/10` handling.

Now: scheme/host/port parsing first, then resolve-all-addresses and validate
every answer, then connect to the validated IP with a pinned
`_create_connection` while keeping the hostname for `Host`/SNI/certs.
Redirects are followed by hand, each hop re-validated. Private redirects are
refused with `502`.

### 5. Header injection returned 502 and leaked the exception

`ref` was copied into the `Referer` header unchecked:

```
before: ?ref=http://foo/%0d%0aX-Evil:%201
        → 502 {"error":"Invalid header value b'http://foo/\\r\\nX-Evil: 1'"}
after:  → 200, injected header absent, Referer falls back to the target host
```

`Range` was forwarded raw too; only a single well-formed `bytes=` range is
forwarded now.

### 6. Worker exhaustion / no useful error mapping

`urlopen(timeout=30)` only bounds individual socket reads. A trickling or
infinite upstream could hold a gunicorn thread indefinitely; a handful of
those starved `/healthz` and the API, so Docker could mark the container
unhealthy and restart it. Upstream 404/403 were reported as `502`, and an HTML
block page could be streamed to the browser as `audio/mpeg`.

Now: per-worker stream slots (`RAVEWEB_MAX_STREAMS`, default 6) answer `503`
with `Retry-After` when full; reads time out after 30 s; a hard duration cap
applies; empty/invalid/non-audio/gzip upstream responses are refused with the
right status (`404`, `403`, `416`, `502`, `504`).

### 7. Front-end hid failures

`navigate()` only rendered on success: a failed section left the previous
section's rows on screen with a 2.6 s toast. `loadMore()` had no error
handling at all (unhandled promise rejection, button stuck). Reloading a
`#/api/filter/kool` URL always went back to Latest because `boot()` ignored
the hash. Rapid navigation could render an older response over a newer one.

### 8. Smaller confirmed issues

- `GET /api/year/9999` and `/api/year/0` returned `200` with an empty list.
- `?page=99999999` returned a `next` link to page 100000000 (`MAX_PAGE=500` now).
- `/api/search?q=…&page=2` produced a malformed next link
  (`?q=kool?page=3`) — found by a test.
- `/api/years` stopped at 1988, not 1987.
- `/api/dnb` called `dnbradio.mixes()` twice and zipped the two results, which
  could pair an id with the wrong title.
- Unknown `/api/*` paths, 405s and 500s returned HTML, not JSON.
- `app.config["JSON_SORT_KEYS"]` is a no-op in Flask 3 (set `app.json.sort_keys`).
- The mobile player hid `.p-extra`, so there was **no way to close the player
  or favourite from a phone**.
- The search input used `font-size: 13px`, which makes iOS zoom on focus.
- The rail's `top: 63px` was hard-coded and overlapped the topbar once it wrapped.
- The favourite heart could only add, never remove, and had no `aria-pressed`.

## Changes ranked by value

1. **Legacy playback works again** (`_split_kodi_target`, `api_stream`).
   Restores an entire section and existing favourites.
2. **SSRF is actually enforced** (`_parse_target`, `_resolve_target`,
   `_open_stream`, `_new_connection`): no scheme escapes, no redirect
   escapes, no DNS rebinding, port/userinfo rules.
3. **Proxy errors are safe and correct** (status mapping, non-audio refusal,
   gzip refusal, sanitised `Range`/`ref`, no internal text in responses).
4. **Workers cannot be starved** (bounded stream semaphore + read/duration/byte
   caps + `503 Retry-After`), and `/healthz` stays answerable under load.
5. **Favourites never 500** (validation, defensive rendering, read-only-safe,
   slash-capable DELETE, honest `removed` flag).
6. **Front-end failure UX** (skeleton loading state, inline error card with
   Retry, 25 s request timeout, offline banner, stale-response guard,
   `loadMore` error/disabled states).
7. **Hash routing and back** (reload keeps the section, back button tracked by
   depth, old percent-encoded hashes still work, nav `aria-current`).
8. **Keyboard + Media Session** (Space, `/`, ←/→/Shift, n/p, q, `?`, Esc;
   lock-screen metadata, seek, prev/next, position state).
9. **Now playing + queue** (playing row highlighted with `aria-current`,
   click-the-playing-row pauses, `n/m` counter, queue panel with jump-to,
   queue survives browsing other sections, load-more appends to the queue).
10. **Mobile/accessibility polish** (player close/favourite visible on phones,
    wrapped topbar, dynamic `--topbar-h`, safe-area insets, 44 px coarse
    targets, skip link, focus-visible rings, `aria-pressed`/`aria-busy`/
    `role=dialog` + focus trap, reduced-motion support).
11. **JSON API hygiene** (short JSON 404/405/413/500, year range 404s, page
    clamp, 200-char search cap, item 404 when there is no audio, `MAX_CONTENT_LENGTH`,
    CSP/`nosniff`/`Referrer-Policy` headers).
12. **Operational** (startup cache pruning, 1 h DNB resolution cache, `/healthz`
    `data_writable`, read-only `/data` no longer crashes at import, CI runs the
    tests, deploy script verifies health and preserves data, image build
    imports the app as a smoke test).
13. **Tests** (153 offline cases; covers every route, favourites lifecycle,
    SSRF matrix, redirects, ranges, proxy errors and static front-end checks).

## What I left alone

- **Provider modules** `core/archive.py`, `radio.py`, `legacy.py`, `feeds.py`,
  `dnbradio.py`, `latest.py` — shared with the add-on and owned elsewhere. No
  edits. Findings below.
- **`static/art/**`** — untouched.
- **Visual design** — kept the existing look; only layout, states, focus and
  touch handling changed.
- **Service worker / offline media cache** — a real feature but a large,
  cache-busting change; the offline banner + retry covers the failure case for
  now.
- **Authentication / per-IP rate limiting** — this is a LAN app; the stream
  slots bound resource use, and exposing 8097 publicly is still a bad idea
  (documented).
- **ICY (`ICY 200 OK`) Shoutcast v1 support** — Python's `http.client` rejects
  that status line; supporting it needs a fragile copy of stdlib internals.
  Behaviour is unchanged from before, and it is documented in the README.
  Icecast/HTTP mounts (tested with four live stations) work.
- **JSON field names, item shapes, `/api/stream?u=&ref=`** — unchanged.
  Additions only: `page=` on search, `data_writable`, `next` on search.
- **`core/cache.py`** — not edited; its TTL semantics were correct and are
  used by the providers. The only gap (expired files never deleted) is handled
  by `_prune_cache()` in `app.py`.
- **`core/kodi.py`** — one edit: `os.makedirs(DATA_DIR)` is now best-effort so
  a read-only `/data` cannot stop the app from importing.

## Provider bugs to report (not edited)

1. **`legacy.playable()`** bakes Kodi header syntax into the URL. It was the
   root cause of broken legacy playback and existing favourites. Better:
   return `{"url": …, "headers": {...}}` and let each front-end apply them.
   Worked around in the proxy.
2. **`archive.audio_files()`** returns `[]` both for "item has no audio" and
   for "metadata fetch failed". The web layer now answers `404` in both cases,
   so a transient Archive outage reads as "no playable audio" instead of a
   source error. Returning `None` on fetch failure would let callers tell the
   difference.
3. **`latest.merge()`** runs five feeds + Archive + DNB sequentially; a cold
   `/api/latest` took >8 s here (each fetch may wait up to 25 s). A small
   thread pool (or shorter per-feed timeouts) would cut the cold start a lot.
4. **`/api/podcasts`** (my route) calls `feeds.episodes(limit=200)` for all
   feeds just to count them, which downloads every feed on a cold visit.
   A cached `count` in `feeds` would avoid that.
5. **`favourites.save()`** writes in place with no atomic rename/fsync. A
   crash or power cut mid-write can truncate `favourites.json`; `load()` then
   returns `[]`, i.e. silent loss. `cache.put()` already does temp-file +
   `os.replace` + fsync; favourites should do the same.
6. **`cache.py`** never removes expired files. Distinct searches create one
   file each, so `/data` grows forever. Pruned at startup from `app.py`, but
   the shared module is the right place.
7. **`radio.stations_by_name()`** can return stations without a stream URL;
   filtered in `app.py`.
8. **`dnbradio.stream_url()`** resolves on every call with no cache; the web
   layer adds a 1 h cache. `mixes()` re-parses the page on every call.

## Verification

```
./.venv/bin/python -m pytest test -q
153 passed in 0.94s

test/test_api.py             62
test/test_favourites.py      26
test/test_stream.py          53
test/test_cache_frontend.py  12
```

Live checks (gunicorn on 127.0.0.1:8098, real network):

```
/healthz            200 {"ok":true,...,"data_writable":true}
/api/menu           200 (11 sections)
/api/latest         200 (cached providers)
/api/filter/kool    200
/api/stream?u=not-a-url                         400 {"error":"blocked or missing url"}
/api/stream?u=file:///etc/passwd                400
/api/stream?u=http://127.0.0.1:8098/healthz     400
/api/stream?u=<archive mp3> Range: bytes=0-1000 206 audio/mpeg, 1001 bytes
/api/stream?u=<legacy mp3|Referer=...> Range    206 audio/mpeg, 1001 bytes (ID3)
/api/stream?u=<radio stream>                    200 chunked, audio bytes (4 stations)
/api/nope                                       404 {"error":"not found"}
POST /api/favourites {"title":{}}               400 {"saved":false,"error":"title is required"}
```

Front-end: `node --check static/app.js` passes. A one-off jsdom harness (not
committed, jsdom installed outside the repo) executed the real `app.js`
against the real template with mocked `fetch`/media and passed 44 checks:
boot/nav, row rendering, playback + queue position, Space pause/resume,
←/→/Shift seeks, `/` search, search navigation, queue panel, hash routing,
browser back, favourite add/remove with `aria-pressed`, error card + Retry,
offline banner, and help open/close. A pytest static check also asserts every
element id used by `app.js` exists in the template.

Not verified live: a public redirector pointing at a private IP (the ones
tried blocked the request). That path is covered by the fake-`302` tests
(`test_redirect_to_private_address_is_refused`,
`test_redirect_to_file_scheme_is_refused`).

## Risks before redeploying

1. **Stream rejection is stricter.** A station that serves `text/html`,
   `application/x-…` or a gzip body is now refused (`502`) instead of being
   played as garbage. Icecast/HTTP MP3 stations tested fine; if one favourite
   station breaks, its content-type needs adding to `_MEDIA_EXACT` in `app.py`.
2. **Port rules.** Ports below 1024 other than 80/443 are refused, plus a
   denylist (22, 3306, 6379, 9200, …). Radio on 8000/8080/8443 is unaffected,
   but a LAN stream on a privileged port will now be blocked.
3. **DNS pinning** connects to the first validated IPv4. If a host has
   multiple A records and the first is down, the proxy does not retry the
   others (it returns `502`). This trades a little availability for rebinding
   safety.
4. **`RAVEWEB_MAX_STREAMS=6` per worker** (2 workers → 12 concurrent streams).
   A busy household could hit `503`; raise it via env if needed.
5. **CSP is now enforced** on `/` and `/static/*`. The current page has no
   inline scripts/styles; adding one later will need a nonce or hash.
6. **Stream duration cap** defaults to 4 h; a longer set would be cut off
   (configurable with `RAVEWEB_STREAM_SECONDS`).
7. **`--max-requests 2000`** recycles gunicorn workers; in-flight streams
   finish gracefully, but expect a brief restart every so often.
8. **Favourites data format is unchanged** and `/data` is preserved by
   `deploy.sh`, so existing favourites survive. The only behaviour change is
   that invalid entries are skipped instead of crashing the list, and unknown
   `kind` values now get `400` (previously stored and ignored).
9. **Cold start is still slow** (provider network calls). The UI now shows a
   skeleton and a Retry instead of looking hung, but `/api/latest` itself is
   not faster on the very first request after a cache wipe.
10. **No Docker build was run here** (no Docker on this machine). The image
    change is limited to gunicorn flags, an `ENV`, and a build-time
    `import app`; the app itself was exercised with the same gunicorn
    invocation locally.
