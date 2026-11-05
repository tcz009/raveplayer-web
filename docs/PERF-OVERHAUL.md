# Performance overhaul — web build (1.6.52 pass)

**Branch:** `perf/overhaul-3.9.49` (off `main` @ 1.6.51, not deployed).
Companion: `../../raveplayer/docs/PERF-OVERHAUL.md` (Kodi add-on; same pass,
same harness conventions).

Targets: **warm API/navigation ≤ 200 ms perceived · cold first paint ≤ 2.5 s for
hubs · RTP category ≤ 3 s · play-start ≤ 1.5 s when cached.**

## Method

- `scripts/bench_api.py` (rewritten) — real Flask views, real disk/in-process
  caches, fake network (`core.fetch._request`, 20 ms per request) so cold vs
  warm is repeatable. It now **logs in as the owner** (the API has required a
  session since 1.6.21, which is why the old script returned 401), replays the
  archived RTP pages, and covers the RTP hub/index/category/post/pack routes,
  JungleTapes, Toronto, Moxser, search and **play-start** (`/api/stream` first
  byte with an in-memory upstream, so it measures proxying + auth, not the
  provider's latency).
- `scripts/bench_sources.py` (existing) — provider-stage split (upstream /
  cache / parse), RTP replayed at 350 ms, other sources live.
- `scripts/bench_ui.py` (fixed) — real app + headless Chrome; it now signs in
  instead of hanging on the login page. Rail interactive / first row / CLS.

Before and after runs were sequential on an idle machine. Raw output:
`docs/bench/api-{before,after}-1.6.52.json`,
`docs/bench/sources-{before,after}-1.6.52.json`.

## API table (ms; 20 ms fake upstream per provider request)

Warm p50 is shown because p95 on a laptop is dominated by scheduler noise.

| Endpoint | Cold before | Cold after | Warm before | Warm after |
|---|---:|---:|---:|---:|
| `/api/menu` | 7.8 | 6.4 | 7.3 | **5.6** |
| `/api/rail/packs` | 6.8 | 6.1 | 7.3 | 5.3 |
| `/api/latest` | 37.6 | 34.4 | 10.5 | 8.9 |
| `/api/filters` | 7.1 | 6.1 | 6.8 | 7.4 |
| `/api/item/pack-0` | 35.1 | 33.4 | 7.9 | 9.9 |
| `/api/moxser` | 2,048 | 2,044 | 7.6 | 6.9 |
| `/api/moxser/<slug>` | 3,784 | **776** | 9.9 | **4.7** |
| `/api/downloads` | 11.7 | 6.2 | 9.4 | **5.9** |
| `/api/search?q=…` | 57.8 | 55.6 | 36.9 | 33.2 |
| RTP hub | 70.3 | 55.9 | 10.6 | 8.4 |
| RTP `/all` | 52.3 | 60.3 | 10.0 | 7.1 |
| RTP index | 52.5 | 56.8 | 10.3 | 12.3 |
| RTP category `/index/events/331` | 86.1 | **47.0** | 18.9 | 16.8 |
| RTP category `/index/djs/106` | 89.2 | **45.2** | 16.7 | 16.0 |
| RTP post `/post/899` | 55.2 | 55.8 | 10.1 | 12.8 |
| `/api/legacy/jungletapes` | 88.9 | 62.5 | 8.7 | 8.3 |
| `/api/toronto` | 9.6 | 5.8 | 8.3 | 6.4 |
| `/api/stream` (first byte) | 10.5 | 31.1¹ | 8.8 | **7.9** |

¹ single cold sample; the stream route's first call initialises the SSRF
resolver and the slot semaphore in the bench process. Warm is the target.

`/api/menu`, `/api/downloads` and `/api/stream` are the rows that show the
one-connection auth change: every request used to open three SQLite
connections (session, block, `last_seen` UPDATE) and now opens one, with the
write throttled to once per 30 s.

## Source table (ms; RTP replayed at 350 ms, other sources live)

| Scenario | Items | Cold before | Cold after | Warm before | Warm after |
|---|---:|---:|---:|---:|---:|
| RTP hub | 5 | 372 | 369 | 8 | **5** |
| RTP `/all` | 17 | 378 | 375 | 8 | 4 |
| RTP index | 209 | 374 | 372 | 8 | 6 |
| RTP category | 3 | 739 | **375** | 23 | **14** |
| RTP post | 48 | 373 | 373 | 7 | 6 |
| RTP pack | 1 | 380 | 374 | 7 | 4 |
| JungleTapes index | 310 | 3,133 | **2,285** | 10 | 7 |
| JungleTapes show | 2 | 974 | 927 | 9 | 4 |
| Toronto index | 4 | 8 | 4 | 7 | 3 |
| Toronto genre | 678 | 1,079 | 1,183¹ | 50 | **19** |
| Moxser index | 1,000 | 2,768 | 3,005¹ | 17 | 11 |
| Moxser detail | 1 | 3,863 | **741** | 10 | **5** |
| Hardscore | 20 | 1,430 | 1,378 | 13 | 12 |
| Archive list | 40 | 972 | 963 | 9 | 7 |
| Archive item | 7 | 951 | 797 | 8 | 6 |
| RTP first track | 48 | 363 | 369 | 0 | 1 |

¹ live-network variance (±30 %); the RTP category, JungleTapes index and
Moxser detail rows are the code changes.

## Rail / first paint (headless Chrome, `bench_ui.py`)

| Run | Rail interactive before | Rail interactive after | First row before | First row after |
|---|---:|---:|---:|---:|
| cold | 106.9 ms | **94.1 ms** | 111.4 ms | **99.6 ms** |
| warm (2 runs) | 110.3 / 119.4 ms | 89.7 / 130.3 ms | 121.1 / 125.5 ms | 95.2 / 133.8 ms |
| CLS | 0.0 / 0.0239 | 0.0 / 0.0246 | — | — |

Boot now starts the first section request and `/api/menu` together instead of
serialising them, which removes one round trip (~10–20 ms here, more on Wi-Fi).
The service worker's stale-while-revalidate shell is not visible in this table
because the bench opens a fresh browser context per run (empty SW cache); a
real repeat launch paints from the cached shell with no network wait.

## Against the targets

| Target | Result |
|---|---|
| Warm navigation ≤ 200 ms | ✅ every warm API is ≤ 33 ms (`/api/search`), most 5–10 ms; rail interactive ~90–130 ms in Chrome |
| Cold first paint ≤ 2.5 s for hubs | ✅ latest 34 ms, RTP hub 369 ms, JungleTapes 2.29 s, Toronto 4 ms (index); ⚠️ Moxser 3.0 s is the site's 1 req/s rate limit (2 sitemap requests), unchanged by design |
| RTP category ≤ 3 s | ✅ **375 ms** cold (one page, was two), warm 14 ms |
| Play-start ≤ 1.5 s when cached | ✅ `/api/stream` first byte 7.9 ms server-side (provider TTFB is separate); the page click is already warm |

## What changed

1. **RTP category no longer fetches the homepage.** The title comes from
   `legacy.cached_categories()` (parse cache, or an already-cached homepage
   body) instead of `legacy.categories()`; a cold category link is one `?cat=`
   page instead of two. `fetch.cached_body()` is the new no-network cache read
   it uses.
2. **Parsed pages persist per exact body.** `core/cache.py` gained
   `parsed()` / `fingerprint()` / `remember(..., sync=False)`; `legacy._cached_parse`
   delegates to it, and `toronto._genre_rows()` caches its ~1 MB page parse
   (warm 50 → 19 ms). Parsed pages already used `sync=False`.
3. **HTTP bodies no longer fsync.** `core/fetch.fetch/fetch_json` cache bodies
   with `sync=False`: recomputable, and an fsync per cold page is a disk stall
   on the NAS host. Fresh `/api/menu`/`/api/downloads` timings include this.
4. **Cold Moxser resolve is one page.** `moxser.detail()` used to build the
   whole sitemap entry list (two rate-limited requests) to recover a page URL
   that is derivable from the slug. It now uses the in-process list when warm
   and otherwise fetches the page directly: cold **3,863 → 741 ms**.
5. **JungleTapes catalogue fetched concurrently.** `_fetch_jt_catalogue()` runs
   `/tapes` + `/sitemap.xml` together (2 workers, order preserved, serial
   fallback): cold **3,133 → 2,285 ms**.
6. **One SQLite connection per request.** `audit.session_check()` replaces
   `session_active()` + `is_blocked()` + `touch_session()` (three connections
   and an UPDATE per API call) with one connection and a `last_seen` write only
   when it is older than 30 s. It is deliberately **not** memoised: a revoke or
   block must deny the very next request (the admin tests pin this).
7. **Versioned static assets are cacheable.** `/static/*?v=<VERSION>` is served
   `public, max-age=31536000, immutable`; un-versioned static (artwork) is
   `public, max-age=300`. API responses stay `no-store`. Browsers no longer
   revalidate the 110 KB app.js on every launch.
8. **Service worker: stale-while-revalidate shell for `/` only.** The app is
   hash-routed, so a launch navigation to `/` returns the cached shell
   immediately and refreshes it in the background (cold/offline still falls
   back to the cache). Other paths (`/login`, `/logout`) stay network-first —
   answering them from the shell would show the app to a signed-out user (the
   browser suite caught exactly that). Cache name bumped to
   `raveplayer-shell-v1.6.52`.
9. **Parallel boot.** `static/app.js` starts the first section request
   concurrently with `/api/menu` instead of awaiting the menu first, and
   re-highlights the rail when it renders.
10. **Bench harnesses fixed.** `bench_api.py` authenticates and replays RTP;
    `bench_ui.py` signs in; both were stale since the 1.6.21 auth change.

## Tests

```sh
cd "Rave Player"
./.venv/bin/python -m pytest test -q      # 513 passed (500 at 1.6.51)
node --check static/app.js
node --check static/sw.js
```

New `test/test_perf_overhaul.py` (13): parsed-once-per-body, Toronto genre
parse-once, RTP category title never fetches (and uses the cached parse),
`fetch.cached_body()` never requests, Moxser detail derives the page URL without
loading sitemaps and rejects hostile slugs, `session_check()` touch window +
block/revoke semantics, and the static/API cache headers. Updated
`test/test_taxonomy_2026.py` (route now uses `cached_categories`) and
`test/test_pwa.py` (stale-while-revalidate markers).

## Residual risks / not done

- Moxser's cold section remains ~3 s: the site asks for ≤1 request/second and
  the index + first sitemap are two requests. Warm is 11 ms.
- `search` warm is ~33 ms with the stubbed providers because Mixcloud is a live
  network call on every search (not cached by design).
- A year-long immutable asset cache means a deploy **must** bump `VERSION`
  (the template appends `?v=`); a hand-edited `app.js` on a running server is
  not picked up by an already-cached browser until the version changes.
- The SQLite connection is still per-request (not pooled/thread-local): the two
  gunicorn workers and WAL make pooling possible, but it was not needed to hit
  the target and would add failure modes to the auth path.
- The browser suite (`test_e2e`, 36 cases) was run: 36 passed after the
  login-navigation fix above.

## Reproduce

```sh
cd "Rave Player"
./.venv/bin/python scripts/bench_api.py --samples 25 \
  --json docs/bench/api-after-1.6.52.json
./.venv/bin/python scripts/bench_sources.py --cold-samples 3 --samples 5 \
  --json docs/bench/sources-after-1.6.52.json
RAVEWEB_PLAYWRIGHT_NO_SANDBOX=1 \
RAVEWEB_CHROMIUM_EXECUTABLE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  ./.venv/bin/python scripts/bench_ui.py
```
