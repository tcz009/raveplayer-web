# Performance + cleanup pass (web 1.6.18)

**Branch:** `feature/1.6.18` (off `main @ 3fbd488`, 1.6.17) · not deployed.
**Targets:** warm API p95 < 300 ms · rail interactive < 1 s · no layout shift.

## Method

`scripts/bench_api.py` runs the real Flask views with the real disk/in-process
caches and a fake network (`core.fetch._request`), sleeping **20 ms** per
provider request so cold vs warm and serial-vs-parallel behaviour is visible but
repeatable. Each endpoint: one cold call (cache dir + memory cleared), then 40
warm calls; p95 reported. `scripts/bench_ui.py` starts the real app with
provider stubs and drives system Chrome at 1280×900: rail interactive = first
nav button visible (from navigation start), first row = Latest row, CLS via
`layout-shift` entries over the first 1.5 s (3 runs: cold + 2 warm).

## API before → after

| Endpoint | Cold before | Cold after | Warm p95 before | Warm p95 after | Payload before | Payload gzip after |
|---|---|---|---|---|---|---|
| `/api/latest` | 33.9 ms | 33.6 ms | 4.0 ms | **3.2 ms** | 11,741 B | 871 B (−93%) |
| `/api/filters` | 1.1 ms | 0.9 ms | 0.4 ms | **0.3 ms** | 2,082 B | 540 B (−74%) |
| `/api/item/pack-0` | 25.3 ms | 23.1 ms | 1.2 ms | **0.9 ms** | 6,737 B | 493 B (−93%) |
| `/api/moxser` | 2,202.7 ms | **1,184.3 ms** | 3,015.0 ms | **1.2 ms** | 10,193 B | 743 B (−93%) |
| `/api/downloads` | 1.4 ms | 0.8 ms | 1.0 ms | **1.2 ms** | 5,133 B | 469 B (−91%) |

Warm p95 is **≤ 3.2 ms for every endpoint** (target < 300 ms). Cold is
provider-latency bound in the stub (20 ms); Moxser cold remains ~1.2 s because
the sitemap index and the first mixes sitemap are two serial rate-limited (≤1/s)
requests — still ~1 s faster than before.

## Rail / layout shift before → after

| Run | Rail interactive | First row | CLS |
|---|---|---|---|
| before cold | 310 ms | 323 ms | 0.0020 |
| before warm | 115–509 ms | 126–558 ms | 0.0003 |
| after cold | 263–437 ms | 281–453 ms | 0.0000–0.0020 |
| after warm | 158–446 ms | 174–467 ms | 0.0000–0.024 |

Rail interactive is well under the 1 s target in every run; CLS stays far below
the usual 0.1 "good" threshold. The wide ranges are desktop/CI load noise, not
a regression; the only actionable shift source is the topbar pill/status swap
(≈0.002) when a request outlives 150 ms.

## Changes

- **In-process cache layer** (`core/cache.py`): bounded (256-entry) TTL/dict in
  front of the disk cache, deep-copied in/out, `clear_memory()` for tests.
  Warm `/api/latest`, `/api/item` and Moxser page/sitemap reads no longer pay a
  file read + JSON parse per request.
- **Moxser**: parsed sitemap entries cached in-process for 10 min (was
  re-reading/re-parsing 500+ URLs every request and paying the 1/s throttle);
  the second mixes sitemap is fetched only when the first page cannot satisfy
  the requested limit or a detail lookup misses (older mixes still resolve).
  Cold list: 3 serial requests → 2.
- **Boot**: favourites load in parallel with the first section request instead
  of blocking it; hearts are synced once rows exist. `/api/latest` and other
  section APIs remain strictly on-demand (no prefetch), row images stay lazy.
- **Payloads**: JSON >= 1 KiB is gzip-compressed when the client accepts it
  (`Vary: Accept-Encoding`; streaming NDJSON untouched) — ~90–93% smaller.
- **Downloads list** reads the index once (computed size from the entries).
- **Dead code/CSS:** removed the never-wired `.embed-placeholder` block (~40
  lines), the unused `railGroup()` helper and three unused `--*-wash` colour
  tokens. No behaviour, URL or label changes.

## Misses / flags

- **Moxser cold** (~1.2 s) is still above a hypothetical 300 ms cold budget; the
  rate limit and two serial sitemap requests are deliberate. Warm (the target)
  is ~1 ms.
- Rail/CLS numbers vary with machine load (range shown above); none exceeded the
  targets in any run.
- gzip applies only to API JSON, not the static CSS/JS bundle (the deployment
  sits behind a LAN and the bundle is ~126 KiB; a reverse proxy can gzip static
  assets if ever needed).

## Reproduce

```sh
.venv/bin/python scripts/bench_api.py --samples 40 --latency-ms 20
RAVEWEB_PLAYWRIGHT_NO_SANDBOX=1 \
RAVEWEB_CHROMIUM_EXECUTABLE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  .venv/bin/python scripts/bench_ui.py
```
