# Rave Player — web build

The Kodi add-on's content sources, served as a browser app so it can run in
Docker on the home server and be used from any device on the LAN.

Self-hosted; examples below use `http://<server>:8097`.

Repo: <https://github.com/tcz009/raveplayer-web> · Kodi add-on: <https://github.com/tcz009/plugin.audio.raveplayer>

## What it does

The rail is **nine grouped buttons** — Latest · Tape Packs · Mixes & Shows ·
Club & Rave Sets · SoundCloud Picks · Live Radio · Curated Gems · Favourites ·
Downloads — and each group opens a hub. That keeps all 22 sections reachable without the old
21-button rail overflowing a 1440×900 window. **Search** stays in the topbar,
**Surprise Me** opens a random tape pack or show (`/api/surprise`), and Back is a
48 px pinned sticker that stays visible while scrolling.

The sections behind the groups:

- **Latest** — newest podcasts, tape packs and DNB Radio shows, merged.
- **Kool FM** — the station live now plus every Kool FM set in the archive.
- **Quick Filters** — 15 classic series and stations: Dreamscape, Helter
  Skelter, Fantazia, One Nation, Centreforce, Don FM, Slammin' Vinyl, DJ EZ,
  Garage Nation, One In The Jungle and more (data in `filters.json`).
- **Live Radio** — seven genre groups from radio-browser.
- **Tape Packs** — the Internet Archive `ravetapes` collection, 1987 to today.
- **By Year** — the same collection browsed by year.
- **Podcasts & Mixes** — present-day shows from RSS feeds (`feeds.json`).
- **DNB Radio** — recent jungle/DnB shows, stream resolved on demand.
- **UK Garage** — DJ EZ and garage sets from Mixcloud plus Archive garage packs
  (garage stations are in Live Radio).
- **Mixcloud Sets** — a discovery hub of club and rave sets, played in
  Mixcloud's own embedded player.
- **SoundCloud Sets** — curated live sets from `soundcloud.json`, played in
  SoundCloud's own embedded player. `/api/soundcloud?q=` adds API text search
  when `SOUNDCLOUD_CLIENT_ID` is set.
- **Moxser Mixes** — underground radio mixes from moxser.com. The server walks
  the site's sitemaps, resolves a mix page to its direct MP3 and plays it
  through `/api/stream`; requests are throttled (≤1/s) and cached (1–6 h).
- **Hardscore** — hardcore/jungle tapes from hardscore.com (direct MP3 when the
  post has one, otherwise the post opens on the source site) plus its event
  listings.
- **Rave Archive UK** — rave tape scans from rave-archive.com; each release
  opens its Mixcloud player in the shared embed panel.
- **GetDarker** — dubstep/grime/140 sets from GetDarker's RSS and curated
  `getdarker` Mixcloud account, played in the Mixcloud widget.
- **Pirate Radio Archives** — a small hand-checked archive of pirate recordings
  (Risk FM 107.1/Beat Sanctuary, Weekend Rush 92.3/Hardscore, Flava FM/A-Team)
  in `resources/pirate-radio.json`, with the same `station, year, title, djs,
  source_url, stream_url, credit` schema as the Kodi add-on.
- **Rave Nights** — the 55-name researched catalogue of promotions, venues and
  pirate stations, opening the Archive packs and Mixcloud sets recorded for
  each.
- **Legacy Sites** — RaveTapePacks.com, JungleTapes.com, Toronto archive.
- **Favourites** — stored server-side in `RAVEWEB_DATA`.
- **Downloads** — explicit, direct-file saves kept under `RAVEWEB_DATA/downloads`
  (Archive tracks, Moxser MP3s, Hardscore MP3/M4A, Pirate Radio Archives direct
  files). Each row shows size/date, plays from the local copy offline, can be
  saved to another device (Content-Disposition + Range) and deleted. Provider
  pages (Mixcloud/SoundCloud/YouTube), live streams, HLS, podcasts, legacy
  scrapers and GetDarker are never downloadable in phase 1; nothing downloads
  automatically. See [`docs/DOWNLOADS.md`](docs/DOWNLOADS.md).


Performance notes and the 1.6.18 before/after numbers live in
[`docs/PERFORMANCE.md`](docs/PERFORMANCE.md) (warm API p95 ≤3.2 ms, Moxser warm
~1 ms, JSON gzip). Benchmarks: `scripts/bench_api.py`, `scripts/bench_ui.py`.

Downloads, section loads and Surprise Me share the Kodi spinning-head art
(`static/art/spinner/`, 10 frames): a progress panel with filename, lime bar,
%, MB done/total, ETA and Cancel; a topbar loading pill that only appears after
150 ms; and the Surprise reveal that spins then stamps the pick. All are CSS
`steps(10)` sprite animations with a reduced-motion fallback — see
[`docs/SPINNER.md`](docs/SPINNER.md).
- **Search** — Archive text search. Paste a Mixcloud or SoundCloud set URL into
  the search box instead and the embed panel opens that set.

The player also shows an AirPlay button in Safari (and a Cast button in
Chromium-based browsers) once a track is loaded. It reports the browser's real
route availability and connection state; it never fakes a "connected" badge.
See [`docs/IOS-AIRPLAY.md`](docs/IOS-AIRPLAY.md) for the iPhone/Range/
receiver-reachability details.

### Mixcloud and SoundCloud embeds

Both set sections play in the providers' own player instead of proxying their
audio: the iframe points at `https://www.mixcloud.com/widget/iframe/?…` or
`https://w.soundcloud.com/player/?…`. Mixcloud answers with a 301 to
`https://player-widget.mixcloud.com/…`, so the page CSP allows exactly those
three framed hosts (`frame-src`); everything else is still refused by
`default-src 'self'`. Pasting either kind of set URL into search opens the same
panel, labelled for the provider, with an "Open on … ↗" link to the original
page. A provider that moves its widget to a new domain needs the CSP entry
updated (and a browser check — the offline tests only assert the header).

## Why a server proxy

`/api/stream` proxies audio. Two reasons:

1. **Hotlink protection** — ravetapepacks.com rejects requests without a
   `Referer`; the browser cannot forge one, the server can.
2. **Mixed content / CORS** — radio streams are plain HTTP, and Archive
   redirects would otherwise be blocked from an HTTPS page.

Range requests are forwarded, so seeking works. The proxy also understands
Kodi's `url|Referer=...&User-Agent=...` syntax, which is how the legacy
provider marks hotlink-protected MP3s.

### Proxy security

`/api/stream` is deliberately a **LAN-only, unauthenticated open proxy**: it can
fetch any *public* http(s) URL so hotlink-protected files, live-radio hosts and
podcast CDNs keep playing. Keep port 8097 on the LAN (never publish or forward
it), or pin the destinations with `RAVEWEB_STREAM_HOSTS` (see the env table) to
turn it into a scoped proxy. Cross-origin reads are blocked: the route sends no
wildcard CORS header and responses carry `Cross-Origin-Resource-Policy:
same-origin`, so other web pages cannot read proxied bytes through a visitor's
browser.

- Only `http` and `https` are accepted. `file:`, `ftp:`, `gopher:` and
  URLs with embedded credentials are refused.
- Every hostname is resolved by the proxy and **all** returned addresses must
  be public. The connection is then pinned to the validated address while the
  original hostname is kept for the `Host` header and TLS SNI, so a second DNS
  answer cannot point the connection at the LAN (DNS rebinding).
- Redirects are followed by hand (max 5) and each hop is re-validated. A
  redirect to a private address is refused.
- Loopback, link-local, RFC1918, CGNAT, multicast, reserved, unspecified and
  IPv4-mapped addresses are refused unless `RAVEWEB_ALLOW_PRIVATE=1`.
- Privileged ports other than 80/443 are refused, and a short denylist blocks
  SSH, SMTP, Redis, Docker, databases and similar ports. Radio on 8000+ works.
- Client headers (`Range`, `Referer`) are validated, never interpolated raw,
  so header injection is impossible.
- A stream is capped by a per-worker concurrency slot, a read timeout and an
  absolute duration. When all slots are busy the proxy answers `503` so the
  API and `/healthz` keep responding instead of being starved of threads.
- Non-audio upstream responses (HTML block pages, gzip) are refused instead of
  being played as if they were MP3.

## Run it

```sh
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m gunicorn -w 2 -k gthread --threads 8 -b 0.0.0.0:8097 app:app
```

Or with Docker:

```sh
docker build -t raveplayer-web:latest .
docker run -d --name raveplayer-web --restart unless-stopped \
  -p 8097:8097 -v "$PWD/data:/data" -e RAVEWEB_DATA=/data raveplayer-web:latest
```

`docker compose up -d` also works where the Compose plugin is installed
(the NAS currently has plain Docker only).

Deploy to a Docker host from a checkout (the script requires an SSH target and
key; nothing about the server is baked into the repo):

```sh
HOST=user@server SSH_KEY=/path/to/private/key ./scripts/deploy.sh
# optional: ALLOW_PRIVATE=1 MAX_STREAMS=8 ./scripts/deploy.sh
```

`deploy.sh` reads `VERSION`, builds `raveplayer-web:<version>` and also tags
it `raveplayer-web:latest`, and records `org.opencontainers.image.version`
plus `org.opencontainers.image.revision` (the checked-out git short sha) as
image labels, so a deployed box can be traced to a commit. It preserves
`/data`, fixes its ownership and polls `/healthz` for 20 s.

### Release note

Web releases are git tags on `main`; there is no release automation.

1. bump `VERSION` and update the docs/test counts;
2. commit on `main` and push;
3. tag `vX.Y.Z` and push the tag:
   `git tag vX.Y.Z && git push origin vX.Y.Z`;
4. deploy from that checkout with `deploy.sh`.

The image tag matches `VERSION`, so rollback is a redeploy of the previous
tag/checkout (check out that tag, run `deploy.sh` again).

The bind-mounted `data/` directory must be writable by uid 10001 (the
container user) — `deploy.sh` fixes that for you. Favourites failing silently
is the symptom when it is not. `/healthz` reports `"data_writable"` so you can
check without guesswork.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `RAVEWEB_DATA` | `/tmp/raveweb` (`/data` in Docker) | Favourites + HTTP cache |
| `RAVEWEB_ALLOW_PRIVATE` | `0` | Set to `1` to allow LAN/Tailscale stream hosts |
| `RAVEWEB_STREAM_HOSTS` | empty (any public host) | Comma-separated host suffixes `/api/stream` may fetch, e.g. `archive.org,rinse.fm,media.rss.com`. Empty keeps live radio working; set it to pin the proxy to known media hosts. |
| `RAVEWEB_DOWNLOAD_BYTES` | `524288000` (500 MiB) | Per-file cap for the Downloads proxy save. |
| `RAVEWEB_DOWNLOAD_TOTAL` | `2147483648` (2 GiB) | Total cap for the Downloads library under `RAVEWEB_DATA/downloads`. |
| `RAVEWEB_MAX_STREAMS` | `6` | Concurrent proxied streams per worker |
| `RAVEWEB_STREAM_SECONDS` | `14400` | Absolute cap on one proxied stream |
| `RAVEWEB_STREAM_BYTES` | `8 GiB` | Byte cap on one proxied stream |
| `RAVEWEB_VERSION` | `VERSION` file | Overrides the version reported by `/healthz` and `/api/menu`, and the `?v=` cache-buster on the page |
| `SOUNDCLOUD_CLIENT_ID` | empty | Enables `/api/soundcloud?q=` API text search; without it only the curated `soundcloud.json` sets are listed |

The data directory is best effort: if it is read-only, favourites still list
and the app keeps working, it just cannot save new ones. Stale cache entries
are pruned at startup (14 days), so a long-lived volume does not fill up.

## API

| Endpoint | Purpose |
|---|---|
| `/api/menu` | sections |
| `/api/latest` | merged newest |
| `/api/filters` | the quick-filter list |
| `/api/packs`, `/api/packs/<key>` | Archive sections (`?page=`) |
| `/api/item/<id>` | tracks in a tape pack |
| `/api/filter/<key>` | one quick filter (Kool FM included) |
| `/api/filter/<key>/live` | live stations for a filter |
| `/api/live`, `/api/live/<tag>` | radio |
| `/api/mixcloud?q=` | Mixcloud search; items carry the widget `embed` URL |
| `/api/mixcloud_hub` | Mixcloud Sets hub (topic folders) |
| `/api/ukg` | UK Garage hub: Mixcloud sets + Archive packs |
| `/api/soundcloud?q=` | curated SoundCloud sets, or API search when configured |
| `/api/raves`, `/api/rave/<key>` | Rave Nights catalogue and one entry |
| `/api/moxser`, `/api/moxser/<slug>` | Moxser mixes; `<slug>` resolves to `{url}` (played via `/api/stream`) |
| `/api/hardscore` | Hardscore mixes (direct MP3 where available) + events |
| `/api/ravearchive`, `/api/ravearchive/<slug>` | Rave Archive UK releases; detail returns the Mixcloud `embed` row |
| `/api/getdarker` | GetDarker sets (Mixcloud `embed` rows, source link fallback) |
| `/api/pirate`, `/api/pirate/<station>` | Curated Pirate Radio Archives: stations then entries |
| `/api/podcasts`, `/api/podcast/<id>` | RSS feeds |
| `/api/dnb`, `/api/dnb/<mix>` | DNB Radio |
| `/api/years`, `/api/year/<year>` | by-year browse |
| `/api/legacy/...` | the surviving sites |
| `/api/search?q=&page=` | Archive search |
| `/api/favourites` (GET/POST) | list / add |
| `/api/favourites/<key>` (DELETE) | remove (key is `kind:target`) |
| `/api/stream?u=&ref=` | Range-capable audio proxy |
| `/api/downloads` (GET/POST) | offline library / explicitly save one direct audio file `{url,title,source}` |
| `/api/downloads/<id>/stream` | play the saved copy offline (Range) |
| `/api/downloads/<id>/file` | save the saved copy to a device (`Content-Disposition`, Range) |
| `/api/downloads/<id>` (DELETE) | delete a saved download |
| `/healthz` | container health + `data_writable` |
| `/manifest.webmanifest` | installable app metadata |

Errors are always JSON for `/api/*` (`{"error": "..."}`) with a meaningful
status: `400` bad/blocked URL, `404` unknown section or upstream media,
`503` too many streams, `504` upstream timeout, `502` upstream failure.
`page` is clamped to 500 so a hand-written `?page=99999999` cannot walk the
Archive forever.

## iPhone and AirPlay

Open the LAN address, not `localhost` or a Tailscale-only `100.x` address: an
AirPlay receiver downloads the stream itself, so it must be able to reach the
server.

- The AirPlay button lives in the player and appears once a track is loaded.
  It is dimmed when WebKit reports no routes; tapping it explains that instead
  of doing nothing. The icon turns cyan only when
  `webkitCurrentPlaybackTargetIsWireless` is true.
- Both the iPhone and the Apple TV / HomePod must be on the same Wi-Fi without
  client isolation. `/api/stream` is Range-capable, which AirPlay needs.
- There is deliberately no volume slider: iOS ignores `HTMLMediaElement.volume`
  and the hardware buttons (or the receiver) own the level.
- Background/lock-screen playback and lock-screen transport controls use the
  Media Session API; live radio omits skip buttons because it has no timeline.
- Layout uses `100dvh` plus safe-area insets, the fixed player height is
  measured rather than hard-coded, and pull-to-refresh is suppressed so a swipe
  cannot reload the app and kill playback.

Full notes, including what was verified without a device, are in
[`docs/IOS-AIRPLAY.md`](docs/IOS-AIRPLAY.md).

## Keyboard shortcuts

| Key | Action |
|---|---|
| `Space` | Play / pause |
| `/` | Focus the search box |
| `←` / `→` | Seek 5 s (with `Shift`: 30 s) |
| `n` / `p` | Next / previous track |
| `q` | Show the queue |
| `?` | Shortcut help |
| `Esc` | Close the queue, help or search |

`Space` on the currently playing row pauses it; each row also has a normal
focusable play button. Phone lock-screen controls work through the Media
Session API.

## Artwork

The 1993 pirate-transmission visual system — icon set, wordmark, palette,
typography, hero and texture language — is documented in
[`docs/ARTWORK.md`](docs/ARTWORK.md). Rebuild every image with
`python3 scripts/make-art.py` (Pillow is a build-time tool only; it is not in
`requirements.txt`).

## Tests

### Offline suite (203 cases)

The offline suite is 203 cases (all providers mocked) and uses the Flask test
client. `pytest` is a test-only dependency:

```sh
./.venv/bin/python -m pip install -r requirements-dev.txt
./.venv/bin/python -m pytest test -q
```

Covers the API routes, favourites lifecycle (including read-only and
failed-write/atomic-save cases), the SSRF rules, redirect handling,
range/header behaviour and every stream error path, cache TTL/pruning, the
radio/podcast TTL caches, the parallel `/api/latest` merge (ordering, failure
tolerance and concurrency), the CSP `frame-src`/embed-host contract, the
search-submit paste wiring, the deploy script's version pinning/labels
(parsed with `sh -n`), and static checks that every element id used by
`app.js` exists in the template and that the JavaScript parses.

### Install on a phone (PWA)

The web app is installable: Safari → Share → **Add to Home Screen**. The
manifest pins `id`/`start_url`/`scope` to `/`, the iOS standalone meta tags are
present, and a root-scoped service worker (secure origins only) falls back to
the cached app shell for any same-origin navigation, never caching
`/api/*` or cross-origin requests. Existing home-screen icons are old Safari
bookmarks — remove and re-add once, as described in [`docs/PWA.md`](docs/PWA.md)
(which also covers the plain-HTTP LAN limitation and what Cockney Player needs).

### Browser smoke tests (18 cases)

`test_e2e/` drives the real app in headless Chromium through Playwright. It
covers what the offline suite cannot: boot with the grouped 9-button rail and
every hub reachable, the rail fitting a 1440×900 window with Favourites and
Downloads visible (the 21-button overflow regression), the pinned back button
staying put while scrolling, Surprise Me opening a random pack/show, the phone
rail/back layout, the Download action → progress panel (save and cancel) → Downloads library →
offline playback → delete round trip (and no Download action on podcast-like
rows), the 150 ms loading pill (fast and slow requests), the Surprise spin/
stamp reveal plus its reduced-motion skip, section navigation rendering every
row, the Mixcloud/SoundCloud embed panel (widget
`src`, provider label, close/backdrop, CSP `frame-src`), normal text search, and
a favourites add/remove round trip. The Flask app runs on a random localhost
port with a temporary `RAVEWEB_DATA`; providers are stubbed and the browser's
external requests are intercepted, so the tests need no internet. Screenshots
are generated with `RAVEWEB_ARTIFACTS_DIR` (see `docs/E2E.md`).

```sh
./.venv/bin/python -m pip install -r requirements-browser.txt
./.venv/bin/python -m playwright install chromium   # add --with-deps on Linux
./.venv/bin/python -m pytest test_e2e -q
```

Playwright is test-only (not in the runtime image or `requirements.txt`). If
the package or its Chromium build is missing, the browser tests skip cleanly;
see [`docs/E2E.md`](docs/E2E.md) for the last full local run (system Chrome in a
sandbox that cannot install Playwright's own Chromium), counts, and the exact
commands.
set `RAVEWEB_REQUIRE_BROWSER=1` to turn that skip into a failure (the CI
browser job does, so a broken install cannot pass silently). In a restricted
local sandbox `RAVEWEB_PLAYWRIGHT_NO_SANDBOX=1` may be needed, and
`RAVEWEB_CHROMIUM_EXECUTABLE=/path/to/chrome` reuses an existing Chrome.

GitHub Actions runs the fast offline job first, then a separate `browser` job
that installs Chromium and runs `test_e2e`; the browser job is gated behind
the offline job so it never slows the main test job down.

See [`docs/CODE-REVIEW.md`](docs/CODE-REVIEW.md) for the review notes and the
changes ranked by value.

## Known limits

- Shoutcast v1 servers that answer with an `ICY 200 OK` status line are not
  supported by Python's `http.client`; use an HTTP/Icecast mount instead.
- HLS (`.m3u8`) stations are proxied but only Safari can play them in a
  `<audio>` element.
- The first cold load of `/api/latest` fetches five RSS feeds, the Archive and
  DNB Radio in a small thread pool, so the wait is bounded by the slowest
  source rather than their sum (still a few seconds on an empty cache).
- Live radio lists and podcast episode counts are cached on disk for 10 and
  15 minutes (`core/radio.LIVE_TTL`, `core/feeds.FEED_TTL`), so a new station
  or episode can take up to one TTL window to appear.
- Mixcloud and SoundCloud sets play in the providers' own iframes. The CSP
  allows exactly the two widget hosts above; a provider domain change breaks
  the panel until the `frame-src` entry is updated.
- No authentication. This is a LAN app: do not publish port 8097 directly.

## Layout

```
app.py              Flask routes + hardened stream proxy
core/               providers shared with the Kodi add-on, plus a Kodi shim
static/             app.css, app.js, artwork
templates/          index.html
test/               offline pytest suite
test_e2e/           headless-browser smoke tests (Playwright)
docs/               code review notes
feeds.json          podcast feeds
filters.json        quick filters
requirements-dev.txt      test-only dependencies (offline suite)
requirements-browser.txt  test-only dependencies (browser smoke tests)
Dockerfile          python:3.12-slim + gunicorn
```
