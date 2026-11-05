# RaveTapePacks cold start — web 1.6.53

Advisor finding P0-1: the hub returned 502 on cold loads because
`RTP_HOMEPAGE_TIMEOUT` was 6 s while the real homepage (201 KB through the
egress proxy) took 5.5–14.3 s, and a 60 s negative cache then made every child
route answer 502/404 for a minute.

## What changed

| Before (1.6.52) | After (1.6.53) |
|---|---|
| `RTP_HOMEPAGE_TIMEOUT = 6` | `30 s` deadline, one bounded retry inside a `45 s` budget |
| Any failed fetch set a 60 s negative mark | Only a **definitive HTTP error** (e.g. the site's 403 bot filter) sets a `15 s` mark; timeouts/transport errors are retried next request |
| Child category/post routes returned `(None, "")` while the mark was set | Child routes always fetch their own page; the hub mark never gates them |
| `post()` returned `None` on a fetch failure → route 404 | `post()` raises `SourceUnavailable` → route 502 `source_unavailable` (the SPA's quiet plate) |
| `RTP_PAGE_TIMEOUT = 6` | `20 s` per page, one retry (30 s budget) |

`core.fetch.last_failure()` exposes the last failure kind on the calling thread
(`http` / `timeout` / `transport`), so `core.legacy` can distinguish "the source
answered" from "the network hiccuped".

## Verification (5 Nov 2026, egress proxy `:8888`)

- 10/10 cold hub requests from 10 fresh `RAVEWEB_DATA` dirs + server restarts:
  `200` in 4.14–5.90 s; every response
  `By Event (50) | By DJ (209) | By MC (90) | By Year (13) | All Packs`.
- `GET /api/legacy/ravetapepacks/post/899` → `200` in 4.16 s, 15 tracks.
- Headless Chromium: clicked the first post track; `/api/stream` resolved,
  `duration 5512.3 s`, `readyState 4`, `currentTime` advanced 0.6 → 3.98 s,
  no console errors.
- Outage server with a dead proxy and no fallback:
  `post/899` → `502 {"code":"source_unavailable"}` in 1.10 s,
  hub → `502` in 1.98 s (never 404).

Tests: `test/test_integration_p0.py` covers the retry/no-negative-mark,
short definitive mark, child independence and post 502.
