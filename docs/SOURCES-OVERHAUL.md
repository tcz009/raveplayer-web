# Sources Overhaul — verification and fixes

**Branch:** `fix/sources-overhaul`
**Kodi:** `plugin.audio.raveplayer` 3.9.49, based on `integration/3.9.48`
(`d44ff38`) · web: `raveplayer-web` 1.6.52, based on `main` 1.6.51 (`a5f6d09`)
**Date:** 2026-11-05
**No deploy:** both trees are local worktrees; no server, container or add-on
zip was pushed.

## Method

- Live HTTP from the real source modules through the homelab RTP egress proxy
  (`RTP_EGRESS_PROXY=http://192.168.1.142:8888`) for every
  `ravetapepacks.com` request; every other provider direct.
- Web: Flask test client with a fresh `RAVEWEB_DATA`/cache/favourites directory
  per run, authenticated as owner, calling the same JSON endpoints the SPA uses.
- Kodi: `test/run.py` (the real `resources/lib/routes.py` handlers against the
  Kodi stubs), plus direct calls into `resources/lib/legacy.py`, `radio.py` and
  `rtp_bridge.py`.
- Streams were checked with a `Range: bytes=0-1023` GET (204/206 with an audio
  content type), not only `HEAD` — JungleTapes answers `412` to a bare HEAD.
- **No live network in the test suite**: every new test replays a fixture or
  monkeypatches the fetch layer. The full suites were run with a cleared Kodi
  test cache.

## What was wrong and what changed

| Area | Defect found | Fix |
|---|---|---|
| JungleTapes show tracks | Every tape side appeared twice: the page repeats each recording in "Stream the Jungle Tape Audio" and in "Download Link(s)" (which also carries OGG copies). | Parse the stream fields only (one pair per side), fall back to the download field if a page has no player, and de-duplicate on a canonical `https://jungletapes.com`/no-`uuid` key. A show that listed 4 rows for 2 sides now lists 2. |
| JungleTapes shows | All 310 sitemap shows were returned in one directory/API response. | Page at 50 (Kodi "Next page ›", web `next`). |
| RaveTapePacks "All Packs" | It parsed the homepage's rotating **RANDOM SELECTION** block, so "All Packs" was a different 8–18 packs on every refresh and real packs were unreachable. | Use the site's blog archive `?cat=0&paged=N` (every post, date order, ~23 pages of 8). Skip the sticky `category-welcome`/`format-status` WELCOME notice and any post with no audio player. |
| RaveTapePacks categories | A cold category view crawled every WordPress page inside one route; long categories could exceed the 20 s route budget and silently show only the first pages. | New `category_page()` fetches exactly one page and returns the next URL; routes expose it as `?page=N` (web `next`, Kodi "Next page ›"). `category()` remains the aggregate helper for tests/screen. |
| RaveTapePacks downloads | The web build had no `ravetapepacks` download source; `_download_allowed()` also rejected the stored `|Referer=…` suffix. | Added `ravetapepacks` to the download allowlist (host + `/music/` path, suffix split before validation), and `_download_chunks()` now rebuilds the Referer/UA from the suffix. Verified live: 12.0 MB pack track saved, Range-served and deleted. Kodi already had the source; re-verified `plan()` is allowed. |
| Favourites (web) | Only archive packs and direct streams could be saved; Toronto/live/pirate/hardscore rows had no `fav`, and RTP post folders/JungleTapes shows/DNB could not be stored. | Added `legacy_post`, `legacy_jt` and `dnb` favourite kinds, validation, list rendering and `fav` fields on the missing rows (RTP post/all rows, JT shows, Toronto tracks, live stations, pirate recordings, Hardscore media, DNB shows). Live round-trip add/list/remove verified for all four kinds. |
| Favourites (Kodi) | New RTP post rows had no pack-level favourite and the old `legacy_pack` keys came from the random homepage. | All Packs/category rows now store a stable `legacy_post` id and `fav_list` opens `legacy_post`; tracks keep the stream favourite. Old `legacy_pack` rows still resolve when the homepage still lists them. |
| Archive search (Kodi) | An unscoped full-text query returned "HARLEM SHAKE POOP", audiobooks and "Lord of the Rings". | Scope to `collection:ravetapes AND mediatype:audio`; when thin, widen with `title:(query)` plus rave/jungle/hardcore/old-skool/garage collections/subjects instead of every audio item. Same widen rule in the web. |
| Pirate Radio Archives | The Kodi data was older than the web data: no Risk FM stream, no notes; web had no Weekend Rush streams. | One merged five-entry catalogue in both native schemas: Risk FM (Beat Sanctuary MP3 + David Morris page-only), Weekend Rush 1993/1995 (Hardscore MP3s), Flava FM (metadata only). Both menus now show the same three stations. |
| Classic Pack Series | Kodi hid Kool FM and Rinse FM and kept a different filter order; web listed all 16. | Kodi now lists all 16 in the web order (Kool, Rinse, Dreamscape, … World Dance). |
| Rave Nights | Kodi dropped the 22 Mixcloud-only rows and sorted by item count; web lists all researched rows alphabetically. | Kodi returns the same 55 rows, alphabetical, with `packs · sets` counts and a Mixcloud hand-off row (when the Mixcloud add-on is installed). |
| Silent RTP failures | An RTP browse failure (blocked/offline) produced an empty folder with no message; an unknown post ended silently. | Kodi now notifies "RaveTapePacks did not answer — try again later." for category/index/all-packs failures and "No tracks on that pack." for an empty post. Playback failures remain quiet by design. |

## Verified matrix (live, 2026-11-05)

Timings are single wall-clock calls from a cold process/cache unless marked
*warm*. The RTP proxy adds ~4–9 s per page; Kodi has a 20 s route budget.

| Source | Menu / page | Result | Timing |
|---|---|---|---|
| Toronto Rave Mixtape Archive | hub (`/api/toronto`) | 4 genres | 7 ms |
| Toronto | Jungle & Hardcore | 678 direct MP3s, all HTTP 200/206 (3 spot-checked) | 1.2 s (1.7 s cold) |
| Toronto | House & Techno | 547 | 1.0 s |
| Toronto | Studio | 1,174 | 1.4 s |
| Toronto | X-Static | 241 | 0.9 s |
| JungleTapes | show list page 1 | 50 shows, `next` page 2 | 2.6 s |
| JungleTapes | show list page 2 | 50 shows, `next` page 3 | 2.6 s |
| JungleTapes | show page (spot-check 3) | 1–2 recordings per show; duplicate-free; stream 206 | 1.1 s |
| RaveTapePacks | hub | By Event 50 · By DJ 209 · By MC 90 · By Year 13 | 4.5 s cold |
| RTP | By Event (Accelerated Culture) | 3 posts; first post 16 tracks, all playable | cat 5.1 s · post 4.4 s |
| RTP | By Event (Amazon) | 8 posts page 1, next page 2 (13 total) | 4.8 s |
| RTP | By DJ (Adam F / Advance / Alex Hazzard) | 3 / 1 / 1 posts; 24 / 15 / 8 tracks | 4.7–9.4 s |
| RTP | By MC (5ive-O / Bassman / Biggie) | 8 / 8 / 6 posts; 16 / 24 / 16 tracks | 6.2–9.5 s |
| RTP | By Year (1992 / 1993 / 1994) | 5 / 8 / 8 posts; 7 / 9 / 6 tracks | 5.2–9.8 s |
| RTP | All Packs page 1 | 8 real packs (WELCOME filtered); first 3 packs 16/16/9 tracks | 5.6 s |
| RTP | All Packs page 2 | 8 packs, `next` page 3 | 4.3 s |
| RTP | Post (`?p=2459`) | 16 tracks; download action present | 3.6 s |
| RTP | Download (web) | 12.0 MB saved through the egress proxy; Range 206; delete OK | 16.2 s |
| Live Radio | hub | Kool / Rinse / Centreforce + 6 genre rows | 9 ms |
| Live Radio | Kool FM | `audio/aacp`, icy-name Kool FM 8 | HTTP 200 |
| Live Radio | Rinse FM | `audio/aacp`, icy-name Rinse FM | HTTP 200 |
| Live Radio | Centreforce 883 | `audio/mpeg`, icy-name Centreforce | HTTP 200 |
| Live Radio | Jungle / Hardcore / DnB / House / UKG / Dubstep | 56 / 115 / 124 / 500 / 23 / 75 stations (directory) | 0.3–0.9 s |
| Mixes & Shows | Podcasts & Mixes | 5 feeds | 1.2 s |
| Mixes & Shows | DNBRADIO | 10 shows | 0.4 s |
| Mixes & Shows | GetDarker (web) | 50 embed/external sets | 1.0 s |
| Mixes & Shows | Moxser | 1,000 mixes | 2.5 s |
| Mixes & Shows | Hardscore (web) | 10 mixes + 10 events | 1.6 s |
| Archive.org search | Web `?q=dreamscape` | 53 rows, all title-matched; no audiobooks in the first 40 | 2.1 s |
| Archive.org search | Kodi `q=dreamscape` | 5 relevant packs, paginated | live query |
| Favourites | Web add/list/remove `legacy_post`, `legacy_jt`, `dnb`, `stream` | saved, listed with correct target, removed | < 50 ms each |
| Downloads | Web archive 6.9 MB file | saved, listed, Range 206, deleted | live |
| RTP bridge (Kodi) | loopback bridge → Range GET | 206 `audio/mpeg`, 1,024 bytes | live |

## Counts and spot-checks

- RTP taxonomy counts: **50 events / 209 DJs / 90 MCs / 13 years** (both apps,
  fixture and live homepage).
- RTP All Packs: 9 posts on page 1 before the WELCOME filter; **8 real packs**
  after; 23 pages available. Spot-checked packs: The Best Of British – The
  Jubilee Jam (16 tracks), Garage Nation – 2nd Birthday (16), Exposure Volume 1
  (9).
- RTP category spot-checks (first post each): Accelerated Culture 16 tracks,
  Amazon 8, Amnesia House 4, Adam F 24, 5ive-O 16, Bassman 24, 1992 7, 1993 9,
  1994 6.
- JungleTapes: 310 sitemap shows (106 current `/tapes` + 204 older sitemap-only,
  all sampled pages return audio). Spot-checked shows: Rinse Out 2000-02-26 (1
  side), 1999-09-25 (2), 1999-09-18 (2); the previously duplicated Assignment
  Earth show is 2 sides, not 4.
- Toronto: 2,640 recordings across the four genre pages.
- Pirate data: 3 stations / 5 entries; Risk FM (1 playable), Weekend Rush (2
  playable), Flava FM (metadata only, hidden in Kodi).

## Remaining limitations (not fixed here)

1. **Kodi GetDarker without the Mixcloud add-on** is still empty: the RSS set
   pages only embed YouTube/Mixcloud/SoundCloud players, so there is no direct
   in-Kodi stream. The web build shows the embeds; Kodi shows the hand-off rows
   only when `plugin.audio.mixcloud` is installed and otherwise says so. This is
   a source/add-on limitation, not a route bug.
2. **Kodi SoundCloud** remains web-only by design; there is no Kodi SoundCloud
   hand-off.
3. **Old Kodi `legacy_pack` favourites** (saved before this change) still resolve
   only while the pack is in the homepage's rotating selection. New packs are
   saved as stable `legacy_post` ids; there is no migration for the old shape.
4. **thepiratearchive.net / piratearchive.co.uk** are still Cloudflare-gated to
   non-browser clients and are not shipped as sources.
5. **Rave Screen's RaveTapePacks tab** still uses the homepage's featured
   (random) packs for its preview rows; playback goes through the plugin/bridge
   correctly. The browsable "All Packs" route is the add-on menu.
6. **Device verification:** no Shield/TV was attached in this session (`adb
   devices` empty), so the Kodi checks were run through the real route code
   with the Kodi stubs and live HTTP, not on a physical player. No deploy was
   performed.

## Tests

| Suite | Command | Result |
|---|---|---|
| Kodi | `python -m pytest -q` | **651 passed** |
| Web | `python -m pytest -q` | **507 passed, 36 skipped** |

New offline fixtures: `test/fixtures/jungletapes-show.html` (real show page
excerpt with stream + download fields) and
`test/fixtures/ravetapepacks-all-packs.html` (blog archive with a sticky
WELCOME post, three audio packs and a pager). New/updated tests cover JT
de-duplication and pagination, RTP all-packs parsing/pagination, per-page
category pagination, RTP download eligibility/suffix handling, the new
favourite kinds, the Kodi search widen query, the merged pirate catalogue and
the 55-row rave catalogue.
