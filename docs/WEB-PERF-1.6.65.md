# WEB-PERF-1.6.65 — Rave Player web performance profile and optimization plan

**Status:** read-only profile of 1.6.64 plus an isolated patch on
`feat/web-perf` (worktree `../rave-player-worktrees/web-perf`, based on
`6eb01db`, the revision the live 1.6.64 image was built from). **Not deployed,
not pushed.** No `app.py`/template changes.
**Measured:** 9 November 2026, 10:35–11:00 UTC.
**Companions:** `docs/PERFORMANCE.md`, `docs/PERF-OVERHAUL.md`,
`docs/RTP-COLD-START-1.6.53.md`.

---

## TL;DR

| Finding | Evidence | Result |
|---|---|---|
| UK Raves' 11 s p95 is a **per-hop TLS handshake problem**, not provider latency | 40 `/go/mix` hooks per page; NAS→ukraves is 73 ms on a reused connection but **99 ms on a fresh one**; a 20-hop 4-worker burst took **2.36 s** with fresh sockets vs **0.26 s** with keep-alive | cold UK Raves page **2.57 s → 0.76 s p50**, **7.46 s → 1.08 s p95** (local, n=9) |
| A warm UK Raves page still resolved every row | 40 `cache.get` calls + one negative-cache re-check per row per request | warm page client **20.7 ms → 10.3 ms**, server handler **9.3 ms → 2.8 ms** |
| One podcast feed deep-copied 4,000 episodes on every `/api/latest` | DejaVu FM parsed list: **55 ms per request** just for the copy | `/api/latest` warm **110 ms → 31 ms**, handler **97 ms → 24 ms** |
| Static assets are **not gzipped** | `app.js` 113,836 B and `app.css` 82,679 B on the wire; gzip middleware only touches `application/json` | ~196 KB per cold device load; gzip ≈ 50 KB (−75%) — **not yet fixed** (needs `app.py` or a proxy) |
| Live has **no production timing data** | `req … ms=` exists at `app.py:3835`, but `app.logger` is WARNING in prod and gunicorn has no access log → **0** `path=` lines in `docker logs` | enable INFO logging before quoting live p95 again |
| EZ Garage and Drive caches are **in-memory only** | `core/ezgarage.py:117`, `core/drive.py:45`; gunicorn runs `--max-requests 2000` with 2 workers | every worker recycle re-pays 1.7 s / 2.3 s; disk persistence is the next P0 |

---

## 1. Method and environment

### Test beds

| Bed | What it is | Notes |
|---|---|---|
| Live NAS | Docker `raveplayer-web` 1.6.64, gunicorn 22 `gthread`, 2 workers × 8 threads, `--max-requests 2000`, 4-core OMV host | LAN-only. **API routes are owner-auth-gated**; see limitation below |
| Local 1.6.64 | Same commit in this worktree, gunicorn 22 `gthread`, 1 worker × 8 threads, macOS, Python 3.9.6 | Used for cold/warm and before/after; provider network is the same LAN |
| Local 1.6.64 base | `../rave-player-worktrees/web-perf-base`, detached at `6eb01db`, identical harness | "before" numbers that are not contaminated by the patch |

The local bed is deliberate: it is the only way to measure **cold** pages
without clearing the live cache (a write to live data). It also lets the
server-side handler time be captured from the existing `req … ms=` log line.

### Harness (added, reusable)

| File | Purpose |
|---|---|
| `scripts/perf_probe.py` | Owner login, then per endpoint: fresh TCP connection, **TTFB**, total, wire bytes, gzip/decoded bytes, p50/p95. `--streams` discovers a real UK Raves / EZ Garage / Mikus id and samples with `Range` so a 300 MB set is never downloaded |
| `scripts/perf_server.py` | Flask dev runner that turns the existing `req … ms=` line on (no `app.py` change) |
| `scripts/perf_wsgi.py` | Gunicorn entry point that sets `app.logger` to INFO for local runs |
| `scripts/warm_ukraves.py` | Operator warmer: parsed-page + provider classification for pages 1..N, off the request path |

```bash
# local bed (fresh cache = cold)
RAVEWEB_DATA=/tmp/rp/data RAVEPLAYER_CACHE_DIR=/tmp/rp/cache \
SECRET_KEY=x RAVEWEB_PASSWORD=y RAVEWEB_ENV=test \
PYTHONPATH=scripts:. .venv/bin/python -m gunicorn \
  -w 1 -k gthread --threads 8 -b 127.0.0.1:28097 perf_wsgi:app

RAVEWEB_TEST_PASSWORD=… .venv/bin/python scripts/perf_probe.py \
  --base http://127.0.0.1:28097 --samples 15 --streams \
  --out docs/bench/perf-warm-1.json
```

Cold = fresh cache directory + fresh worker. Warm = one warm-up request then
15 samples. Percentiles are nearest-rank.

### Live limitation (important)

The live container has only `RAVEWEB_PASSWORD_HASH` /
`RAVEWEB_FRIENDS_PASSWORD_HASH`; the plaintext is not on the host. Session
validity requires a row in `/opt/raveplayer-web/data/auth.sqlite3`
(`core/audit.py:session_check`), so a test session cannot be minted without
either the owner password or writing to live data. Both were out of scope for
this read-only pass. The live numbers in this report are therefore:

* unauthenticated `/healthz` and header/TLS checks;
* **provider-side** timings measured from the NAS host itself (same network
  path the container uses), which is what separates "server time" from
  "provider time";
* read-only inspection of the live cache directory and container logs.

**Do not present the local p50/p95 numbers as live SLA figures.** They are
route+provider measurements on identical code; the NAS is slower on SQLite and
disk but has the same network path.

---

## 2. Baseline (1.6.64, before the patch)

### 2.1 Cold — fresh cache, fresh worker (local, n=1 per endpoint)

| Endpoint | p50 ms | TTFB ms | Wire B | Decoded B |
|---|---:|---:|---:|---:|
| `/healthz` | 5.3 | 5.2 | 52 | 52 |
| `/` shell | 27.2 | 27.1 | 16,382 | 16,382 |
| `/api/menu` | 30.8 | 30.6 | 1,561 gz | 6,582 |
| `/api/latest` | **4,564.4** | 4,557.8 | 2,926 gz | 16,246 |
| `/api/ukraves?page=1` | **2,570.2** | 2,558.6 | 1,391 gz | 11,164 |
| `/api/ukraves?page=2` | 1,823.5 | 1,822.8 | 1,485 gz | 13,308 |
| `/api/ukraves?page=3` | 1,898.5 | 1,897.6 | 1,634 gz | 13,630 |
| `/api/ezgarage` | 15.6 | 15.4 | 1,485 gz | 8,837 |
| `/api/mikus` | 1,611.6 | 1,611.3 | 2,187 gz | 7,613 |
| `/api/downloads` | 9.6 | 9.5 | 198 | 198 |
| RTP hub | **9,762.7** | 9,762.1 | 798 | 798 |
| RTP `events/331` | 6,219.5 | 6,218.6 | 368 gz | 1,072 |
| RTP `djs/106` | 7,166.1 | 7,165.3 | 593 gz | 2,581 |
| RTP `/all` | **15,223.3** | 15,222.3 | 521 gz | 2,299 |
| `/static/app.js` | 28.3 | 28.2 | **113,836** | 113,836 |
| `/static/app.css` | 9.0 | 8.9 | **82,679** | 82,679 |
| UK Raves stream (Range) | 1,243.3 | 1,073.4 | 64 KiB | — |
| EZ Garage stream (Range) | 1,730.5 | 1,004.1 | 64 KiB | — |
| Mikus Drive JSON | **2,329.7** | 2,329.3 | 188 | 188 |
| Mikus Drive stream (Range) | 1,471.1 | 1,288.5 | 64 KiB | — |

RTP rows are inflated by the local → LAN egress-proxy path; the NAS-side proxy
measurement in §3.2 is the honest provider number. Everything else is
LAN-comparable.

### 2.2 Warm — 15 samples, one worker (local, before patch)

| Endpoint | p50 ms | p95 ms | TTFB p50 |
|---|---:|---:|---:|
| `/api/menu` | 24.1 | 32.0 | 23.7 |
| `/api/latest` | **110.0** | 116.3 | 109.8 |
| `/api/ukraves?page=1` | 20.7 | 35.2 | 20.0 |
| `/api/ukraves?page=2` | 20.1 | 21.7 | 19.9 |
| `/api/ukraves?page=3` | 23.7 | 32.8 | 23.3 |
| `/api/ezgarage` | 16.4 | 20.6 | 16.0 |
| `/api/mikus` | 27.6 | 37.5 | 27.3 |
| `/api/downloads` | 14.5 | 21.4 | 14.5 |
| RTP hub | 16.3 | 20.7 | 16.2 |
| RTP category | 38.7–43.2 | 43.1–68.6 | 38.5 |
| `/static/app.js` | 8.0 | 20.8 | 7.8 |
| UK Raves stream (signed URL cached) | 731.7 | 1,004.1 | 566.4 |
| EZ Garage stream (signed URL cached) | 750.6 | 891.9 | 579.6 |
| Mikus Drive JSON (URL cached) | 18.2 | 34.2 | 18.1 |
| Mikus Drive stream | 1,466.3 | 1,879.8 | 1,283.2 |

Server handler times from the same run (`req … ms=`):

| Route | handler p50 | handler p95 |
|---|---:|---:|
| `/api/menu` | 3.4 ms | 6.8 ms |
| `/api/latest` | 96.9 ms | 229.1 ms |
| `/api/ukraves` | 9.3 ms | 29.7 ms (cold outliers included) |
| RTP category | 28.9 ms | 6.2 s (cold) |

### 2.3 UK Raves cold distribution (the 14/169 pages > 5 s)

Nine cold page measurements across three fresh boots (pages 1–3):

```
before: 1768, 1824, 1899, 2546, 2570, 2633, 2909, 3139, 7464 ms
        p50 2570 · p95 7464 · max 7464
after:   692,  696,  712,  728,  760,  878,  889,  906, 1076 ms
        p50  760 · p95 1076 · max 1076
```

This reproduces the live shape: the body is usually 2–3 s, with a tail of
5–8 s (and on the NAS, 11–12 s). The tail is not a provider outage; it is 40
fresh TLS handshakes at four-way concurrency plus the occasional slow hop
approaching the 20 s `DESCRIBE_DEADLINE`.

### 2.4 Live state observed (read-only)

| What | Value |
|---|---|
| `/healthz` | `{"ok":true,"version":"1.6.64","data_writable":true}` |
| Container | healthy, gunicorn 2×8 gthread, `--max-requests 2000 --max-requests-jitter 300` |
| Cache size | 62 MB, 4,612 JSON entries |
| Cache composition | **338 parsed UK Raves pages** (169 app pages × 2 categories), **3,809 Mediafire redirects**, 6 YouTube, 1 empty, ~370 raw HTML bodies (≈ 30 MB) |
| Cache age | 4,562 entries written in the previous hour (a full-catalogue warm happened around 10:02–10:37; no app code does this) |
| Request logs | **zero** `path=` lines: the timing logger exists but never runs at INFO |
| Provider reachability from NAS | ukraves 200, Mediafire 200, Drive 200, RTP only through `RTP_EGRESS_PROXY` (direct = TCP timeout) |

The full UK Raves catalogue was warm *during this pass*, so a live 11 s p95
taken now would not be comparable. The 6 h TTLs mean the cold behaviour
returns daily; the patch targets exactly that.

---

## 3. Server time vs provider time

### 3.1 UK Raves — provider is fast, the client shape is wrong

Measured from the NAS host (same path as the container):

| Probe | Result |
|---|---|
| Category page (downloads) | 0.25 s TTFB / 0.29 s total, 113 KB |
| Category page (other-downloads) | 0.21 s / 0.24 s, 85 KB |
| One `/go/mix/<id>` on a **fresh** connection | 73–99 ms |
| Same 20 hops, 4 workers, fresh connections each | **2.36 s** total, p50 0.408 s, p95 0.634 s |
| Same 20 hops, one keep-alive connection per worker, 4 workers | **0.26 s** total, p50 0.035 s, max 0.087 s |
| Serial 20 hops, keep-alive | 0.61 s, p50 0.028 s |
| Serial 20 hops, new connection each | 2.32 s, p50 0.099 s |
| 8 workers, keep-alive | 0.32 s (no better than 4) |

So the provider costs ~73 ms per hop and four-way concurrency makes the
server-side tail 0.4–0.6 s per hop. Forty hops at four workers is 4–6 s;
a slow hop or two and the page is 7–12 s. **A keep-alive socket removes the
handshake, not the request**: after the patch, the same 40 hops cost ~0.3–0.5 s.

### 3.2 RTP — egress-proxy bandwidth, not server code

Measured from the NAS through the configured `RTP_EGRESS_PROXY`:

| Page | TTFB | Total | Bytes |
|---|---:|---:|---:|
| Homepage `/` | 2.40 s | 6.81 s | 200,809 |
| `?cat=331` | 1.20–2.12 s | 5.13–5.18 s | 155,513 |
| `?cat=331&paged=2` | 2.12 s | 4.54 s | 87,018 |
| Direct (no proxy) | — | TCP timeout | — |

The site does **not** gzip (same bytes with `--compressed`). Cold RTP is
therefore `proxy RTT (~1.2–2.4 s) + 150–200 KB at ~30–60 KB/s (~3–5 s)`.
Warm routes are 39–43 ms (one disk read + parse). The app's reported 2.5–4 s
is the optimistic end of this; `/all` (200 KB) is the 9–15 s case.

### 3.3 Drive — two probes, every worker start

| Stage | Time |
|---|---:|
| `drive.google.com/uc` → `drive.usercontent` (first probe, HTML) | 1.01 s |
| confirm-form second probe (audio, 206) | 1.20 s |
| Full `curl -L` equivalent from NAS | 0.45 s |
| App `drive.resolve()` cold (local) | 2.33 s |

The gap is Python-side: urllib opens a fresh TLS connection for each probe
(one to `drive.google.com`, one to `drive.usercontent`), plus
`parse_confirm_form`. Resolved URLs are cached **in memory only** for 2 h
(`core/drive.py:44–45`), and `_LOCK` serialises every Drive resolve in the
worker.

### 3.4 EZ Garage — in-memory only, globally serialised

One Mediafire page fetch measured from the NAS: 0.28 s TTFB / 0.43 s total.
Local cold stream: 1.73 s (resolve 0.3 s + upstream first byte 0.7 s + 64 KB).
The live 4–8 s is consistent with a Mediafire 429 backoff or the 1.2 s
`MIN_GAP` after another resolve, not with server CPU. The cache is in-memory
only (`core/ezgarage.py:117`), so a worker recycle or the 2,000-request cap
turns the next stream back into a cold resolve.

### 3.5 `/api/latest` — provider-bound cold, copy-bound warm

Cold 4.56 s is the slowest provider in the 7-way merge (Archive/feeds/DNB).
Warm was **97 ms of handler**, dominated by one deep copy: DejaVu FM's parsed
list has 4,000+ episodes and `cache.get` deep-copied it on every request
(55 ms), before `episodes(limit=6)` sliced six rows off it.

---

## 4. Code review — waste found

Numbers are measured, not inferred, unless marked "estimate".

| # | Issue | Location | Evidence / impact | Status |
|---|---|---|---|---|
| 1 | **New TLS connection per `/go/mix` hop** | `core/ukraves.py` `_fetch_location` (old urllib opener) | 99 ms vs 28 ms per hop; 20-hop burst 2.36 s vs 0.26 s | **fixed** |
| 2 | **Warm page re-resolves all 40 rows** | `describe_many` called per request | 40 `cache.get` + negative-cache refetch; 20.7 ms → 10.3 ms client after | **fixed** |
| 3 | **Deep copy of a 4,000-row feed list** | `core/cache.py:163` + `core/feeds.py:episodes` | 55 ms per `/api/latest`; handler 97 → 24 ms | **fixed** |
| 4 | Raw HTML body **and** parsed rows cached for every UK Raves page | `ukraves.page_entries` (`http.fetch(ttl=TTL)` + `cache.remember`) | ~370 raw bodies ≈ 30 MB of 62 MB; both TTL 6 h, so the raw body is almost never read before it expires | patch plan |
| 5 | 256-entry memory cache vs 40 redirect keys/page | `core/cache.py:35` | A single page touches 42 keys; ~6 pages evict the whole layer; every later `cache.get` is a file read | patch plan |
| 6 | No single-flight on cache misses | `cache.remember` | Two gunicorn workers can fetch the same cold page twice; 40 duplicate hops per user pair | patch plan |
| 7 | EZ Garage signed URL is in-memory only | `core/ezgarage.py:117` | Lost on worker recycle (`--max-requests 2000`) and deploy; cold 1.7 s (local) / 4–8 s (live) returns | patch plan |
| 8 | Drive URL is in-memory only + one global lock | `core/drive.py:44–45, 247` | Cold 2.33 s; all users serialised behind one Drive resolve | patch plan |
| 9 | RTP cold pages are proxy-bound with a 6 h TTL | `core/legacy.py` `PACKS_TTL/CATEGORY_TTL` | 5–9 s per cold page; no warmer; `/all` 200 KB | patch plan |
| 10 | Static JS/CSS **not compressed** | `app.py:65–88` gzip only when `response.mimetype == "application/json"` | 196 KB per cold device load; ~50 KB gzipped (estimate, −75%) | patch plan |
| 11 | Shell HTML (16 KB) not compressed | same after_request | 4 KB gzipped estimate; affects first paint on Tailscale/mobile | patch plan |
| 12 | Room JSON has no ETag/304 | all API routes: `Cache-Control: no-store` | Every navigation re-renders and re-sends 1.5–16 KB; a 304 would save the render | patch plan |
| 13 | `Vary: Cookie` on static assets | `app.py` after_request (global) | Defeats any shared cache/CDN for immutable assets; browser cache still works via `?v=` | note |
| 14 | Immutable static caching **is already correct** | `app.py:3795–3805` | `?v=1.6.64` → `public, max-age=31536000, immutable` + `ETag` + `Last-Modified` (verified) | done |
| 15 | HTTP/1.1 only; 6 requests per navigation | gunicorn 22 (no HTTP/2), no TLS proxy | Head-of-line blocking on one connection; Tailscale/mobile RTT amplifies | patch plan |
| 16 | Session SQLite connection + `commit()` on every API request | `core/audit.py:209–244`; `_session_valid` | One connection per request; `last_seen` write throttled to 30 s (good), but `conn.commit()` runs even with no write | micro-fix |
| 17 | 20 s `DESCRIBE_DEADLINE` can hold a request for 20 s | `core/ukraves.py:55` | Observed max 7.46 s cold; deadline is the worst-case tail | keep (defensive) |
| 18 | Per-row fav/download dicts | `app.py:api_ukraves` loop | 40 rows → 1.4 KB gzipped; handler 2.8 ms warm — **not a bottleneck** | no action |
| 19 | Mikus large Drive download 504 at ~90 s | `app.py:_serve_stream`, `_download_chunks`, `READ_TIMEOUT=30` | Not reproducible without the specific file/live session; 4 h stream deadline and 500 MB cap rule out the obvious constants | needs instrumentation |

---

## 5. Implemented on `feat/web-perf` (isolated, tested)

### 5.1 Changes

1. **`core/ukraves.py` — keep-alive `/go/mix` resolution.**
   `_connection()` keeps one `http.client.HTTPSConnection` per resolving
   thread; `_fetch_location()` reads the status/`Location`, drains the small
   redirect body and returns the socket to the thread-local. A dropped socket
   is retried once on a fresh connection; a body over 64 KiB drops the socket.
   Unexpected hosts still use the old one-shot urllib path, so the helper did
   not become a general crawler. `_REDIRECT_SLOTS` (4) is unchanged.

2. **`core/ukraves.py` — per-page provider map.**
   `describe_many()` first reads one small map keyed by the page's id set
   (`ukraves:described:v2:<fingerprint>`, 10 min TTL). Rows in the map are
   returned without touching `redirect_target`; only unknown rows enter the
   pool. `unavailable` results are deliberately not memoised, so the existing
   5 min negative mark still governs retries and a row that starts working is
   not hidden for the longer TTL.

3. **`core/cache.py` — opt-out of the defensive deep copy.**
   `cache.get(..., copy_value=False)` returns the shared in-process object.
   Default remains `True`; the disk path still returns a fresh object.

4. **`core/feeds.py` — read the parsed episode list without copying it.**
   The list is only sliced/iterated by `/api/latest` and `/api/podcast`; a cold
   miss builds a private list and `put` deep-copies it into the memory layer.

5. **Tooling:** `scripts/perf_probe.py`, `scripts/perf_server.py`,
   `scripts/perf_wsgi.py`, `scripts/warm_ukraves.py`; raw runs in
   `docs/bench/perf-*.json`.

### 5.2 Before → after (local, same harness)

| Route | Before | After | Change |
|---|---:|---:|---:|
| UK Raves page cold, p50 (n=9) | 2,570 ms | **760 ms** | −70% |
| UK Raves page cold, p95 (n=9) | 7,464 ms | **1,076 ms** | −86% |
| UK Raves warm page, client p50 | 20.7 ms | **10.3 ms** | −50% |
| UK Raves warm page, handler p50 | 9.3 ms | **2.8 ms** | −70% |
| `/api/latest` warm, client p50 | 110.0 ms | **31.3 ms** | −72% |
| `/api/latest` warm, handler p50 | 96.9 ms | **24.0 ms** | −75% |
| DejaVu feed read (in-process) | 55 ms | **0.1 ms** | −99% |
| Warm-up cost per page (`warm_ukraves.py`) | — | 0.61–0.65 s | 20 pages ≈ 13 s |

Cold UK Raves after the patch is now **page-fetch bound** (2 × 0.2–0.25 s
from the provider + ~0.3–0.5 s for 40 keep-alive hops + parse/JSON).

### 5.3 Tests

* `test/test_ukraves_perf.py` (new, 7 tests): described-map reload skips all
  rows; an unavailable row is retried and not hidden; different id sets do not
  share a map; one connection per thread; keep-alive reuse; reconnect after a
  dropped socket; non-ukraves hosts keep the urllib path.
* `test/test_cache.py`: shared read returns the same object, default read
  still deep-copies, a caller mutation cannot leak through the default.
* `test/test_more_sources.py` / `test/test_sources.py` / `test/test_cache.py`
  all green.
* Full offline suite: **850 passed** (`python -m pytest test -q`), 2 min 09 s.

### 5.4 Risk notes

* Thread-local sockets live for the life of the request's worker thread
  (one page's worth of hops). A server-side idle close is handled by the
  one-retry path; there is no unbounded pool.
* The described map changes **no visible behaviour**: the same `provider`
  dicts feed the same route filter (YouTube/external still dropped).
* `copy_value=False` is used only by `feeds.episodes`; documented in the cache
  helper and covered by a mutation test.

---

## 6. Proposed fixes (not implemented unless marked)

### P0 — do next, low risk

1. **Persist EZ Garage / Drive signed URLs to the disk cache (2 h TTL).**
   `cache.get("ezgarage:signed:v1:<id>", 2h)` / `cache.put(..., sync=False)`;
   same for `drive:url:v1:<id>`. Keep the global lock for the Mediafire gap,
   but check the disk entry before the network. Negative results stay
   in-memory (5 min).
   *Expected:* first byte after a worker recycle **4–8 s → ~0.7 s** (provider
   first byte remains); Drive JSON **2.1 s → ~20 ms**. *Risk:* low; the
   in-memory TTL already uses the same 2 h assumption.
2. **Enable INFO request logging in production.**
   Either a gunicorn `--log-config` that sets the `app` logger to INFO, or a
   `RAVEWEB_LOG_LEVEL` env read in `app.py` (one line). *Expected:* every
   request gets `req id=… path=… ms=…`, giving real p50/p95 per route and
   the server/provider split without guesswork. Zero behaviour risk.
3. **Run `warm_ukraves.py --pages 20` after deploy/restart** (and from a
   6-hourly timer). *Expected:* removes the 0.6–1 s cold page from the first
   visitor; 20 pages ≈ 13 s, 169 pages ≈ 110 s.

### P1 — high value, moderate risk

4. **Static + shell gzip.** Extend the after_request to compress
   `text/css`, `application/javascript`, `text/html` when the client accepts
   gzip, or pre-compress at build and serve `.gz`. *Expected:* 196 KB → ~50 KB
   for the two bundles, 16 KB → ~4 KB for the shell. Watch ETag/Content-Length
   and do not gzip the NDJSON stream.
5. **ETag/304 for room JSON.** Hash the JSON body, set `ETag`, honour
   `If-None-Match` with 304 and `Cache-Control: private, max-age=0,
   must-revalidate`. *Expected:* repeat navigations skip ~1.5–16 KB and the
   Python render; biggest on `/api/latest` and UK Raves.
6. **RTP warm + longer TTL.** Warm the homepage and the first page of the top
   ~10 Event/DJ categories after deploy (same egress path); consider
   `CATEGORY_TTL` 6 h → 24 h (site updates daily). *Expected:* cold 5–9 s
   becomes a warm 40 ms for the pages people open; halves the number of cold
   windows. Do not raise it before the warmth exists.
7. **Drop the duplicate raw UK Raves HTML cache.** Fetch category pages with
   `ttl=0` and keep only the parsed page + described map.
   *Expected:* −30 MB cache, fewer NAS disk writes; no latency change.
8. **Byte-budget memory cache (or skip large raw bodies).** Replace the
   256-entry cap with a ~32 MB LRU budget, and never put raw HTML bodies in
   memory. *Expected:* warm page reads stay in memory across more pages;
   removes 40 disk reads per page after navigation.

### P2 — infra / larger

9. **HTTP/2 + gzip + TLS at the edge** (Caddy, as already used for Cockney
   Player): one connection multiplexes the shell's ~6 parallel API requests,
   serves gzipped static with HTTP/2, and keeps gunicorn HTTP/1.1 behind it.
   *Expected:* removes head-of-line on high-RTT (Tailscale/mobile); also fixes
   P1 #4 without touching `app.py`.
10. **Single-flight `cache.remember`.** Per-key in-process lock (bounded map)
    so two threads do not resolve the same cold page twice; cross-worker
    dedupe would need a file lock or a shared store.
11. **Mikus 504 instrumentation.** Log phase transitions (`resolve`,
    `upstream headers`, `first chunk`, `bytes/sec`) with the request id, and
    reproduce with the exact large Drive file. Current constants (4 h stream
    deadline, 500 MB download cap, 30 s read timeout) do not explain 90 s.
12. **Trim the per-request SQLite commit** when `session_check` performed no
    `UPDATE`. Micro (sub-ms on the NAS), but it is one write transaction per
    API request.

---

## 7. Live verification plan (when an owner password is available)

```bash
# 1. Login-based p50/p95 (read-only GETs; password passed via env, never stored)
RAVEWEB_TEST_PASSWORD=… .venv/bin/python scripts/perf_probe.py \
  --base http://<nas>:8097 --samples 10 \
  --only "ukraves,latest,ezgarage,mikus,rtp,static" \
  --out docs/bench/perf-live-1.6.65.json

# 2. Cold UK Raves after the patch: pick a page whose 6 h entry has expired
#    (or wait for TTL). Expect p1 ≤ 1.2 s and warm ≤ 30 ms.
RAVEWEB_TEST_PASSWORD=… .venv/bin/python scripts/perf_probe.py \
  --base http://<nas>:8097 --samples 1 --warmup 0 --only ukraves

# 3. Production timing lines (after P0 #2)
ssh <nas> 'docker logs --since 10m raveplayer-web | grep "path=/api/"'
```

Acceptance targets for 1.6.65:

| Metric | Target |
|---|---|
| UK Raves cold page p95 | ≤ 1.5 s (from 7.5–12.3 s) |
| UK Raves warm page p95 | ≤ 50 ms (from 11 s p95) |
| `/api/latest` warm p95 | ≤ 80 ms (from 116 ms local / unknown live) |
| EZ Garage stream cold after one warm | ≤ 1.5 s (from 4–8 s) |
| Drive JSON warm | ≤ 50 ms (from 2.1 s) |
| Static cold bytes | ≤ 60 KB (from 196 KB) |

---

## 8. Caveats

* Live authenticated routes were **not** measured (see §1). The report does
  not claim a live p50/p95 for UK Raves, EZ Garage, Drive, RTP or Mikus.
* The live UK Raves cache was almost fully warm during this pass, from a crawl
  that is not part of the app. The 6 h TTLs mean the cold behaviour returns
  daily; the patch and the warmer target that window.
* Local RTP numbers are inflated by the Mac → LAN proxy hop; §3.2 uses
  NAS-side measurements instead.
* The NAS is a 4-core OMV host with SQLite on the same disk as 62 MB of cache;
  local handler times are a lower bound for NAS CPU/disk time, not a
  like-for-like SLA.
* `docs/bench/perf-*.json` are raw harness output from the runs quoted above;
  they include the local-only `mikus drive` id and the discovered stream ids.
