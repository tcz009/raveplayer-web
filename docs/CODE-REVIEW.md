# Independent code review — brief

**Project:** Rave Player — web app (`raveplayer-web`)
**Version under review:** 1.6.25 (`feature/1.6.25-admin-write`, based on the 1.6.24 quick wins)
**Reviewer target:** `app.py`, `core/`, `static/`, `templates/`, `test/`; ignore `data/`, `__pycache__/`, `.venv/`.

## What it is

A small Flask app that mirrors the Kodi add-on: browse/stream oldskool rave tape packs, podcasts and live pirate radio from archive.org, radio-browser.info, RSS, Mixcloud and SoundCloud. Streams are proxied to the browser; no content is hosted.

- Python 3.12 in the container (`python:3.12-slim`), Flask 3.0.3, gunicorn 22.0.
  (The local `.venv` used by the test command below is older; the container is the
  deployment target. CI runs the suite on 3.12.)
- Docker image runs as uid 10001, `no-new-privileges`, bound to a single port (8097).
- Deployed via `scripts/deploy.sh` (build on host, restart container, health check at `/healthz`).
- Tests: `pytest` — `test/` (API, archive, cache, deploy, favourites, speedups, stream, ukg).

## How to run tests

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q test
```

A prepared `.venv` exists in this checkout.

## Layout

| Path | Purpose |
|---|---|
| `app.py` | all HTTP routes (menu, latest, filters, item, live, search, mixcloud/soundcloud, favourites, stream, health) |
| `core/fetch.py`, `core/cache.py` | bounded fetch + on-disk cache |
| `core/archive.py` | archive.org search/item/audio resolution (same logic as add-on) |
| `core/radio.py` | live stations (radio-browser + curated Kool/Rinse) |
| `core/feeds.py`, `core/dnbradio.py`, `core/mixcloud.py`, `core/soundcloud.py`, `core/legacy.py` | sources |
| `core/favourites.py` | server-side favourites in `/data` |
| `static/`, `templates/` | vanilla JS SPA |
| `scripts/deploy.sh` | Docker deploy |

## Changes since last review

- **Live radio:** curated Kool London + Rinse FM streams; radio-browser results filtered; new `rinse` section/filter (1.6.10).
- **Archive playback:** item audio now uses the item’s own storage node (`d1`/`server`/`d2` + `dir`) rather than the `archive.org/download/` redirect, which intermittently hit a node returning HTTP 500 (1.6.11).
- **1.6.12 hardening pass** (`docs/HARDENING.md`): provider redirect SSRF guard + response
  body caps/log redaction (`core/fetch.py`), favourites read-modify-write lock, deploy
  rollback + env-file secrets, radio mirror/search/podcast latency bounds, `/api/stream`
  wildcard CORS removed (+ optional `RAVEWEB_STREAM_HOSTS`), stale `Content-Length`
  dropped, cache pruning.
- **1.6.13 independent-review follow-up** (same doc): duration bounds (no 500 on huge
  colon-form metadata; Python 3.12 int limits), wall-clock deadline on provider body
  reads and redirect hops, deploy rollback for `docker run` failures with trap-based env
  cleanup, plus the review's P3s (raves counts, `MERGE_WORKERS`, full-document entity
  scan, feed http(s)-only media URLs, cache entry hardening, DNBRADIO dates, legacy
  path/dedupe parity, `/api/ukg` cleaning, off-thread prune).
- **1.6.14 new mirror sources** (`docs/HARDENING.md` §1.6.14): Moxser
  (`core/moxser.py`: sitemap index → mixes sitemaps → mix page → direct MP3,
  cached/throttled/fail-soft), Hardscore (`core/hardscore.py`: RSS + events,
  direct media or link-out), Rave Archive UK (`core/ravearchive.py`: RSS +
  Mixcloud embed), GetDarker (`core/getdarker.py`: RSS embeds + `getdarker`
  Mixcloud account) and the curated Pirate Radio Archives
  (`core/pirate.py` + `resources/pirate-radio.json`). New endpoints
  `/api/moxser[/<slug>]`, `/api/hardscore`, `/api/ravearchive[/<slug>]`,
  `/api/getdarker`, `/api/pirate[/<station>]`; SPA sections wired in `app.js`
  with a safe http(s)-only external-link fallback (no innerHTML, CSP unchanged).
- **1.6.15 rail / chrome / theme** (`docs/THEME-WEB-2026.md`): the 21 flat rail
  buttons became **8 grouped buttons** with server-rendered hubs
  (`/api/rail/<id>`), fixing the overflow that hid Favourites/Search at
  1440×900; larger pinned back button (48 px, sticky); `GET /api/surprise` for
  Surprise Me; and a brutalist/blung CSS pass mirroring the Kodi 2026 tokens
  (ink/paper/acid/magenta/cyan, Impact display, halftone + scanlines). Logo,
  wordmark and character unchanged; no innerHTML, CSP unchanged; section/rail
  paths are cross-checked between Python and `app.js` in tests.
- **1.6.16 Downloads/offline (web side, phase 1)** (`docs/DOWNLOADS.md`): explicit
  direct-file saves for the four eligible sources (Archive, Moxser, Hardscore,
  Pirate) through a proxy that reuses `_open_stream` (SSRF/DNS pinning/redirect
  checks), per-file/total/free-space guards, atomic writes and safe filenames;
  offline library with size/delete/play (Range) and a Content-Disposition save
  endpoint. No provider pages/live/HLS/podcasts/legacy/GetDarker; no
  auto-download; no CSP or innerHTML change.
- **1.6.17 spinner pass** (`docs/SPINNER.md`): the Kodi 3.9.19 head frames
  (10 × 128/256 px, copied verbatim) are played as a CSS `steps(10)` sprite;
  the download response now streams NDJSON progress/saved/cancelled/error lines
  to a panel (filename, %, size, ETA, Cancel with a server-side cancel set), a
  150 ms-delayed topbar loading pill covers every `api()` call, and Surprise Me
  spins then stamps its pick. Reduced motion disables every animation and skips
  the reveal wait. No innerHTML/CSP change; no new fetch path (the progress
  stream reuses the same guarded download helper).
- **1.6.18 performance + cleanup** (`docs/PERFORMANCE.md`): bounded in-process
  cache layer in front of the disk cache; Moxser sitemap entries cached with
  pagination skipped when unnecessary; favourites load in parallel with the
  first section; gzip for API JSON ≥1 KiB (≈90–93% smaller); downloads index
  read once; dead `.embed-placeholder` CSS, `railGroup()` and unused tokens
  removed. Warm API p95 ≤3.2 ms, Moxser warm ~1 ms, rail <1 s, CLS ≤0.024.
  No URL/label/behaviour change.
- **1.6.19 PWA/home-screen fix** (`docs/PWA.md`): manifest `id`/`start_url`/
  `scope` pinned to `/` with `no-cache`; iOS standalone meta tags; root-scoped
  service worker that handles only same-origin GETs, network-first navigations
  with cached-shell fallback, never caches `/api/*`/cross-origin, and static
  assets by versioned URL; registration only in secure contexts. Fixes iOS
  "Add to Home Screen" saving a stale/redirected Safari tab as a bookmark.
- **1.6.21 owner/friends login** (`docs/AUTH.md`): fixed `owner` (admin) via
  `RAVEWEB_PASSWORD_HASH` and `friends` (viewer) via
  `RAVEWEB_FRIENDS_PASSWORD_HASH`; session stores `identity` + `role`, reusable
  `require_admin` decorator with `/api/admin/whoami` probe; per identity+IP
  lockouts and independent hash rotation; generic login errors, audit lines
  with identity/role, CSRF, signed `HttpOnly`/`SameSite=Lax` cookie (`Secure`
  over HTTPS), 12 h/30 d lifetimes; all pages/APIs/downloads gated; designed
  responsive login page. Arbitrary accounts/OAuth deferred.
- **1.6.20 web login**: single-operator groundwork above (now extended).
- **1.6.22 owner stats** (`docs/ADMIN.md`): persistent SQLite audit/session
  store (`RAVEWEB_AUTH_DB`, default `$RAVEWEB_DATA/auth.sqlite3`) recording
  login/failure/lockout/logout/CSRF events, identity/role/IP/user-agent/
  endpoint/bytes; session rows with sid validity. Read-only
  `GET /api/admin/stats` for owner session or `RAVEWEB_WIDGET_TOKEN` bearer
  (friends 403): sessions now, 24 h logins/failures/lockouts/unique IPs/
  requests/bytes, 7-day bandwidth, top endpoints, last IPs. Revoke/block/
  friends-actions intentionally deferred, no UI.
- **1.6.23 security fix**: `/api/admin/stats` no longer returns raw session
  ids; `sessions.items[].session_id_masked` is a short non-reversible SHA-256
  prefix for display. Tests assert the raw sid is absent from the JSON.
- **1.6.23 background downloads** (`docs/DOWNLOADS.md`): persisted
  `POST/GET /api/downloads/jobs`, per-job cancel, in-process workers capped at
  1–2, ownership (owner all / friends own), restart recovery, completion into the
  normal index; UI background action, topbar chip, live Downloads rows/cancel and
  completion toast. Same direct-source/host/cap guards as foreground saves.
- **1.6.24 quick wins**: shared SQLite login-failure/lockout counters (both
  workers enforce one policy); themed 404/500 pages (API stays JSON); CSP
  report-only header + public `/api/csp-report` endpoint/logging (enforcing CSP
  unchanged; promotion documented in `docs/CSP-REPORTING.md`); iPhone QA runbook
  (`docs/QA-IPHONE.md`).
- **1.6.25 owner write API** (`docs/ADMIN-WRITE-API.md`): masked-session
  list/revoke, IP/identity blocks with TTL and session revocation, persisted
  friends disable/enable, CSRF on all mutations, 30/min admin rate limit, audit
  rows, stats `blocked_now`/`recent_actions`; friends 403, widget token 401.
- Tests: `205 → 354` offline (`pytest -q test`) and **21 browser e2e**
  (`pytest test_e2e`, see `docs/E2E.md`).

## Areas worth scrutiny

- **`/api/stream` / playback proxies.** The app can be asked to fetch third-party URLs (favourites, SoundCloud/Mixcloud resolution). Check for SSRF, redirect handling, content-type/size limits, and whether private addresses are blocked (there is a `RAVEWEB_ALLOW_PRIVATE` flag — verify its default and coverage).
- **Path/identifier handling.** Archive identifiers and filenames arrive from query params and metadata; check quoting, traversal, and cache-key construction.
- **Blocking I/O.** Routes are synchronous Flask handlers; archive metadata calls can take seconds. Check timeouts and worst-case request latency.
- **Server-side favourites** are stored under `/data`; check file writes, locking, and input sanitisation.
- **No authentication by design** (LAN use). Confirm there is nothing destructive exposed, and that the Docker bind defaults are safe.
- **HTML/label injection** into the SPA from third-party titles (escaping in `static/app.js`).
- **Docker/deploy script**: secret handling (env), volume ownership, health check, rollback.

## Questions for the reviewer

1. SSRF / open-proxy risk in stream and resolution endpoints — is the allow/deny logic sound?
2. Path traversal or cache poisoning via identifiers/filenames?
3. Are all outbound calls bounded and fail-safe (no hung workers)?
4. Favourites data integrity under concurrent requests?
5. XSS in the SPA from third-party titles/URLs?
6. Anything in `deploy.sh` that could expose secrets or leave the container in a bad state?

Findings can be returned as a file or pasted notes; we will triage every item.
