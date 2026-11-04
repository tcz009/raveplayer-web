# Legacy source loading — RaveTapePacks first (web)

**Branch:** `perf/legacy-source-loading` (not merged, not deployed)
**Code commit:** `6fcae11` · **Report:** this commit (`docs/PERF-SOURCES.md` + `docs/bench/`)
**Priority:** RaveTapePacks.com (RTP); JungleTapes, Toronto, Moxser, Hardscore
and archive.org are measured as baselines.

## Outcome

- RTP **pack detail** dropped from **1,131 ms cold** (3 upstream pages) to
  **396 ms** (1 page): a category link now resolves from the category page and
  never parses the homepage. Warm 24 ms → **12 ms**.
- RTP **category page** dropped from **750 ms cold** (2 pages) to **396 ms**
  (1 page). Warm 17 ms → **10 ms**.
- **First-track resolution** 1,123 ms → **381 ms** cold, 19 ms → **1 ms** warm.
- Parsed RTP pages are cached by body fingerprint (6 h pages, 6 h category
  pages); a warm request no longer re-runs the three regex passes. The fixture
  page is 200 KB / 138 tracks, so this is 10–40 ms of CPU per request saved.
- Track/pack names are decoded **once**; a literal `&amp;` or `%20` in a
  filename is no longer corrupted by the display pass.
- Toronto/Moxser cache hits no longer sleep for the courtesy gap: web
  `toronto_genre` warm **1,005 ms → 49 ms**, `moxser_detail` **1,004 ms → 12 ms**.
- `/api/legacy/ravetapepacks*` responses are 691 B–26 KB (largest: 209-row DJ
  index, 26,431 B). The default stays the complete list; `?limit=&offset=` is
  now available opt-in and returns `next` when more rows exist.
- RTP page fetches are **single-flight per host with a 1 s gap** (no bursts,
  one page at a time). No same-host parallelisation was added: the serial cost
  was duplicate pages, not independent ones. Cross-source pools already exist
  (`/api/latest` 7 workers, feeds counts 4, search 3).

## Blocked: RaveTapePacks IP ban

This network is still blocked by RaveTapePacks. DNS resolves
`www.ravetapepacks.com` to `94.126.20.188`, but TCP 443 and 80 both time out
(`curl` connect timeout, `nc -z` fails); the committed audit reports:

```
ravetapepacks  0/0 pass (0%)
fetch failed: https://www.ravetapepacks.com/ (<urlopen error timed out>)
```

Therefore **all RTP numbers below are a replay**, not a live measurement:

- `test/fixtures/bench/rtp-home.html` — archived `https://www.ravetapepacks.com/`
  (Wayback `20240809012921`, 200,366 B, 17 packs / 138 tracks / 362 categories).
- `test/fixtures/bench/rtp-cat-331.html` — archived `?cat=331` (155,050 B,
  3 packs / 48 tracks).
- Replay serves those exact bodies through the real fetch/cache/parse stack with
  **350 ms simulated upstream latency** per page.

Provenance and checksums: `test/fixtures/bench/PROVENANCE.md`. Replay cannot
verify TLS, redirects, the live parser or the live ban. When the ban lifts:
`scripts/bench_sources.py --replay none` and
`scripts/audit_legacy_links.py --source ravetapepacks`.

The baseline sources are live measurements (they are reachable).

## Live link audit (committed script)

`scripts/audit_legacy_links.py` sampled playable URLs with the same
Referer/User-Agent the app sends and a 2-byte Range:

| Source | Sample | Result |
|---|---:|---|
| JungleTapes | 5 | **5/5 (100%)**, 206 `audio/mpeg` |
| Hardscore | 5 | **5/5 (100%)**, 206 `audio/mpeg` |
| RaveTapePacks | — | **0/0**, homepage connect timed out (IP ban) |

```sh
./.venv/bin/python scripts/audit_legacy_links.py --source jungletapes --limit 5
./.venv/bin/python scripts/audit_legacy_links.py --source hardscore --limit 5
```

## Before → after

All times are wall-clock ms for the real Flask route (test client, real
caches, real JSON). `cold` clears the process and disk cache; 3 cold samples for
RTP, 1 for the live baselines, 5 warm samples. p50/p95. "Parse" is everything
that is not a text fetch: Flask/JSON serialisation plus, for endpoints with a
JSON-level cache (archive.org search), that cache read.

### RTP (replay)

| Scenario | Rows | Cold p50 before | Cold p50 after | Cold p95 before | Cold p95 after | Warm p50 before | Warm p50 after | Warm p95 before | Warm p95 after |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| hub `/api/legacy/ravetapepacks` | 5 | 366 | 388 | 369 | 390 | 12 | 11 | 12 | 12 |
| all `/all` | 17 | 396 | 405 | 396 | 406 | 18 | **9** | 28 | 10 |
| index `/index/djs` | 209 | — | 386 | — | 387 | — | 11 | — | 13 |
| category `/index/events/331` | 3 | 750 | **391** | 755 | 392 | 17 | **9** | 23 | 9 |
| pack detail (category link) | 16 | 1,131 | **391** | 1,131 | 393 | 24 | **9** | 37 | 11 |
| first track (`playable()`) | 1 | 1,123 | **381** | 1,123 | 381 | 19 | **1** | 30 | 1 |

The hub cold p50 difference is one fetch either way and is run noise; the
category/pack/first-track rows are the changes (one and two fewer page fetches).
The index row was added with this pass, so it has no "before".

### Baselines (live)

| Scenario | Rows | Cold p50 before | Cold p50 after | Warm p50 before | Warm p50 after | Warm p95 before | Warm p95 after |
|---|---:|---:|---:|---:|---:|---:|---:|
| `jungletapes` index | 310 | 2,940 | 2,579 | 14 | 14 | 18 | 17 |
| `jungletapes` show | 2 | 1,024 | 1,037 | 10 | 10 | 14 | 13 |
| Toronto index | 4 | 8 | 10 | 7 | 9 | 8 | 10 |
| Toronto jungle page | 678 | 1,028 | 1,168 | 1,005 | **48** | 1,035 | 49 |
| Moxser index | 1,000 | 2,218 | 2,440 | 13 | 15 | 24 | 25 |
| Moxser detail | 1 | 4,148 | 3,732 | 1,004 | **12** | 1,006 | 14 |
| Hardscore index | 20 | 1,548 | 1,432 | 15 | 16 | 18 | 19 |
| archive.org list | 40 | 1,042 | 939 | 11 | 12 | 23 | 13 |
| archive.org item | 7 | 1,086 | 786 | 11 | 10 | 13 | 12 |

Cold numbers are live-network bound and move ±30%; the warm Toronto/Moxser
rows are the cache-hit fix (they used to sleep for the 1 s courtesy limit even
on a disk-cache hit). JungleTapes/Hardscore/archive.org are unchanged within
noise.

## Bounded concurrency / politeness

RTP page fetches are now a **per-host single-flight** slot with a 1 s gap
(`core/fetch.POLITE_HOSTS`), entered only on a cache miss. Two requests can
never hold two RTP pages open at once, and a warm page returns without sleeping.

No 2–4 worker pool was added **for RTP pages**: the serial calls were duplicate
fetches of the same homepage, which are removed rather than raced. The same-host
"one page at a time" rule in the brief rules out parallel category prefetching.
Independent *sources* already run in bounded pools (`/api/latest` 7 workers,
feed counts 4, search 3); the benchmark deliberately runs one source at a time
so its numbers are not contention noise.

## Response size / row count

| Endpoint | Rows | Raw JSON |
|---|---:|---:|
| `/api/legacy/ravetapepacks` | 5 | 793 B |
| `/api/legacy/ravetapepacks/all` | 17 | 3,056 B |
| `/api/legacy/ravetapepacks/index/djs` | 209 | 26,431 B |
| `/api/legacy/ravetapepacks/index/events/331` | 3 | 691 B |
| `/api/legacy/ravetapepacks/<pack>?cat=331&kind=events` | 16 | 6,729 B |

The API already gzips JSON ≥1 KiB, so the 26 KB index is ~3 KB on the wire.
The complete list remains the default (LIST-CAPS.md); `?limit=&offset=` is
opt-in for clients that want a smaller first response, and returns `next`.
First paint is not blocked by a full-catalogue parse: the hub only parses the
nav (1 ms); the 200 KB pack parse happens on `/all` or a pack detail.

## What changed (web)

- **`core/legacy.py`**
  - one `_homepage()` fetch with `PAGE_HEADERS`; `packs()` and `categories()`
    now share one cache entry. Previously `packs()` sent the default
    `RavePlayer/3.0` UA, so it cached under a different header key: pack detail
    fetched the homepage twice (once per header set) plus the category page.
  - `_cached_parse()` caches the parsed packs/categories/category page for 6 h,
    keyed by a stable BLAKE2b fingerprint of the exact body. A changed body
    (test fixture, upstream edit) re-parses; the same body skips all regex work.
  - `category_packs()` validates the id as digits and fetches `?cat=` directly
    instead of first resolving it through the homepage nav.
  - `pack()` resolves a category-linked pack from the category page, falling
    back to the homepage index; the app route calls it.
  - `_display()` stops re-running `html.unescape`/`unquote` on names the URL
    resolver already decoded once.
- **`core/fetch.py`**
  - `_host_slot()`: per-host single-flight + 1 s gap for RTP (cache misses only).
  - `is_cached()`: providers with a courtesy gap can skip it on a cache hit.
- **`core/cache.py`** — `has()` checks freshness without deep-copying the body.
- **`core/toronto.py`, `core/moxser.py`** — throttle only on a cache miss.
- **`app.py`** — `_optional_slice()` + `limit`/`offset` on the RTP all/index/
  category routes; pack lookup delegates to `core.legacy.pack()`.
- **`scripts/bench_sources.py`** (new) — source timing harness, web and Kodi.
- **Fixtures** — archived RTP replay pages + provenance.

## Reproduce

```sh
cd "Rave Player"
./.venv/bin/python -m pytest test -q          # 483 passed
node --check static/app.js

# RTP replay (same bytes, 350 ms simulated upstream)
./.venv/bin/python scripts/bench_sources.py \
  --only rtp_hub,rtp_all,rtp_index,rtp_category,rtp_pack,rtp_first_track \
  --cold-samples 3 --samples 5 --json /tmp/rtp.json

# Everything, RTP replayed + baselines live
./.venv/bin/python scripts/bench_sources.py \
  --cold-samples 1 --samples 5 --json /tmp/all.json

# Live RTP once the ban lifts
./.venv/bin/python scripts/bench_sources.py --replay none --only rtp_hub,rtp_pack
./.venv/bin/python scripts/audit_legacy_links.py --source ravetapepacks --limit 5
```

Raw benchmark output: `docs/bench/before-web.json`, `after-web.json`,
`before-rtp-replay.json`, `after-rtp-replay.json`.

## Tests added

`test/test_legacy_cache.py` (9): parse-once per body, re-parse on body change,
browser headers on every homepage fetch, category view makes one request,
non-numeric category ids never fetch, category-first pack lookup, homepage
fallback, category parse cache, literal `&amp;`/`%20` names.
`test/test_taxonomy_2026.py` (+2): complete-by-default + opt-in slicing for the
index and all routes. `test/test_cache.py` (+1): `has()` freshness.
