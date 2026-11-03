# Web scene taxonomy — 1.6.44

Implements the independent 2026 scene-taxonomy review (labels, grouping and
exposure only; no new scraping). The full review lives in the workspace
knowledge base; this file is the shipped contract.

## Rail

`Latest · Tape Packs · Pirate Radio · Mixes & Shows · Live Radio · Favourites ·
Downloads` (Search stays in the topbar). `Club & Rave Sets`, `SoundCloud Picks`
and `Curated Gems`/the 2014 bucket are gone.

## Tape Packs — exact order

1. **Toronto Rave Mixtape Archive** → `/api/toronto` (Jungle, Hardcore & Happy
   Hardcore; House, Techno & Trance; Studio; X-Static)
2. **JungleTapes.com** → `/api/legacy/jungletapes`
3. **RaveTapePacks.com** → `/api/legacy/ravetapepacks` — its own **By Event
   (50)**, **By DJ (209)**, **By MC (90)**, **By Year** and All Packs indexes.
4. **Classic Pack Series** → `/api/filters`
5. **By Rave, Venue & Promoter** → `/api/raves`
6. **By Year** → `/api/years`
7. **Every Pack** → `/api/every-pack` (the archive.org collection, demoted from
   row 1; sub “Archive mixtapes”).

The duplicate `legacy_toronto` route and the whole Legacy Sites bucket are
removed. `/api/legacy/toronto` is 404; `/api/legacy` is 404.

## Mixes & Shows

`Podcasts & Mixes · DNBRADIO · GetDarker · Moxser` (Toronto and Hardscore leave
this group).

## Pirate Radio

`Hardscore · Pirate Radio Archives · Kool FM Sets`. Hardscore is promoted out of
Mixes & Shows; Rave Archive UK/SoundCloud/GetDarker old “sets” grouping is
retired from the rail.

## Live Radio

Named stations first — **Kool FM · Rinse FM · Centreforce 883** (all 200/audio
verified by the reviewer; Centreforce is added to the curated station table) —
then **Hardcore · Jungle · Drum & Bass · House · UK Garage · Dubstep & Grime**.
`Oldskool & Rave` (an era, not a genre) and `Breakbeat` (ambiguous) are removed.

## Genre doors

`Jungle` and `Drum & Bass` are separate doors; `Hardcore` and `Happy Hardcore`
are separate doors. Archive query labels/ids are split accordingly, and
`Oldskool & Rave`/`Breakbeat` no longer exist in the live list.

## Contract tests

`test/test_taxonomy_2026.py` asserts exact rail/hub order and paths, split
genre labels, live station-then-genre order, kebab-case section ids, the
RaveTapePacks 50/209/90/13 nav counts, By DJ/By MC routes and the removed
legacy bucket/Toronto route. `test_e2e/test_smoke.py` checks the taxonomy rail
and Tape Packs order in both dark and light themes at desktop and phone sizes.
