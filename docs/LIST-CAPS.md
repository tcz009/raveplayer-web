# Web source listing caps — 1.6.42

Audit of every browsable source after the hidden-limit report. Default routes
now render the complete source list (or use the existing `next`/“Load more”
pagination); no source silently truncates at an arbitrary constant.

| Source | Old cap | 1.6.42 behaviour |
|---|---|---|
| Moxser Mixes | 48 default / 200 max | Complete sitemap-index set (all mixes pages, deduped, newest first). Internal `limit=` remains for tests/detail only. Sitemaps cached 1 h, mix pages 6 h, ≤1 req/s. |
| Hardscore mixes | 30 default / 50 max | All RSS mix items. |
| Hardscore events | 10 default / 20 max | All event-feed items. |
| Podcasts & Mixes episodes | 60 | All episodes in the feed document. |
| Toronto Rave Mixtape Archive | 60 default / 200 route cap (module 1000) | All rows on each genre page; `?limit=` is an optional explicit cap, not a default. Pages cached 6 h, ≤1 req/s. |
| Rave Archive UK releases | 30 default / 50 max | All RSS release items. |
| GetDarker sets | 30 default / 50 max | All RSS set items; Mixcloud-account fallback now requests up to 200 (was a hard 48). |
| Pirate Radio Archives | curated finite | All curated entries (no cap, unchanged). |
| Internet Archive sections | 40 per page | Already paginated with `next` + total from upstream; complete via “Load more”. |
| DNB Radio / radio | page/API bounded | Provider lists are unpaged single pages or per-filter station lists; no arbitrary local slice. |

Why no huge single response for everything? These lists are bounded by the
upstream documents/sitemaps (hundreds, not unbounded), and all expensive fetches
are cached and shared. The existing Archive pagination remains for the genuinely
large collection. `?limit=` is still accepted on Toronto for explicit callers;
it never silently defaults.

Tests: `test/test_list_caps.py` constructs >old-cap fixtures for Moxser (75),
Hardscore (45 mixes + 25 events), podcast episodes (70), Rave Archive (45),
GetDarker (45), Toronto (1001), and asserts both module and `/api/...` row
counts equal the upstream fixture count.
