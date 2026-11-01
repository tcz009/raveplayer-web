# Independent code review — brief

**Project:** Rave Player — web app (`raveplayer-web`)
**Version under review:** 1.6.13 (`hardening/1.6.13`, based on `main` @ `ee9b77c` / 1.6.12)
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
- Tests: `205 → 257` offline (`pytest -q test`) and **7 browser e2e** (`pytest test_e2e`,
  see `docs/E2E.md`).

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
