# Rave Player — web build

The Kodi add-on's content sources, served as a browser app so it can run in
Docker on the home server and be used from any device on the LAN.

Live on `server-host`: <http://192.0.2.10:8097>

Repo: <https://github.com/tcz009/raveplayer-web> · Kodi add-on: <https://github.com/tcz009/plugin.audio.raveplayer>

## What it does

- **Latest** — newest podcasts, tape packs and DNB Radio shows, merged.
- **Kool FM** — the station live now plus every Kool FM set in the archive.
- **Quick Filters** — Dreamscape, Helter Skelter, Fantazia, One Nation,
  Centreforce, Don FM, Slammin' Vinyl (data in `filters.json`).
- **Live Radio** — six genre groups from radio-browser.
- **Tape Packs / By Year / Search** — the Internet Archive `ravetapes`
  collection, 1987 to today.
- **Podcasts & Mixes** — present-day shows from RSS feeds (`feeds.json`).
- **DNB Radio** — recent jungle/DnB shows, stream resolved on demand.
- **Legacy Sites** — RaveTapePacks.com, JungleTapes.com, Toronto archive.
- **Favourites** — stored server-side in `RAVEWEB_DATA`.
- **AirPlay / Cast** — the player shows an AirPlay button in Safari (and a Cast
  button in Chromium-based browsers) once a track is loaded. It reports the
  browser's real route availability and connection state; it never fakes a
  "connected" badge. See [`docs/IOS-AIRPLAY.md`](docs/IOS-AIRPLAY.md) for the
  iPhone/Range/receiver-reachability details.

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
./.venv/bin/pip install -r requirements.txt
./.venv/bin/gunicorn -w 2 -k gthread --threads 8 -b 0.0.0.0:8097 app:app
```

Or with Docker:

```sh
docker build -t raveplayer-web:1.0 .
docker run -d --name raveplayer-web --restart unless-stopped \
  -p 8097:8097 -v "$PWD/data:/data" -e RAVEWEB_DATA=/data raveplayer-web:1.0
```

`docker compose up -d` also works where the Compose plugin is installed
(the NAS currently has plain Docker only).

Deploy to the NAS from a checkout:

```sh
SSH_KEY=~/.ssh/key-file ./scripts/deploy.sh
# optional: ALLOW_PRIVATE=1 MAX_STREAMS=8 ./scripts/deploy.sh
```

The bind-mounted `data/` directory must be writable by uid 10001 (the
container user) — `deploy.sh` fixes that for you. Favourites failing silently
is the symptom when it is not. `/healthz` reports `"data_writable"` so you can
check without guesswork.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `RAVEWEB_DATA` | `/tmp/raveweb` (`/data` in Docker) | Favourites + HTTP cache |
| `RAVEWEB_ALLOW_PRIVATE` | `0` | Set to `1` to allow LAN/Tailscale stream hosts |
| `RAVEWEB_MAX_STREAMS` | `6` | Concurrent proxied streams per worker |
| `RAVEWEB_STREAM_SECONDS` | `14400` | Absolute cap on one proxied stream |
| `RAVEWEB_STREAM_BYTES` | `8 GiB` | Byte cap on one proxied stream |

The data directory is best effort: if it is read-only, favourites still list
and the app keeps working, it just cannot save new ones. Stale cache entries
are pruned at startup (14 days), so a long-lived volume does not fill up.

## API

| Endpoint | Purpose |
|---|---|
| `/api/menu` | sections |
| `/api/latest` | merged newest |
| `/api/packs`, `/api/packs/<key>` | Archive sections (`?page=`) |
| `/api/item/<id>` | tracks in a tape pack |
| `/api/filter/<key>` | quick filters (Kool FM included) |
| `/api/filter/<key>/live` | live stations for a filter |
| `/api/live`, `/api/live/<tag>` | radio |
| `/api/podcasts`, `/api/podcast/<id>` | RSS feeds |
| `/api/dnb`, `/api/dnb/<mix>` | DNB Radio |
| `/api/years`, `/api/year/<year>` | by-year browse |
| `/api/legacy/...` | the surviving sites |
| `/api/search?q=&page=` | Archive search |
| `/api/favourites` (GET/POST) | list / add |
| `/api/favourites/<key>` (DELETE) | remove (key is `kind:target`) |
| `/api/stream?u=&ref=` | Range-capable audio proxy |
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

The suite is offline (all providers mocked) and uses the Flask test client.
`pytest` is a test-only dependency:

```sh
./.venv/bin/pip install -r requirements-dev.txt
./.venv/bin/python -m pytest test -q
```

Covers the API routes, favourites lifecycle (including a read-only data dir),
the SSRF rules, redirect handling, range/header behaviour and every stream
error path, cache TTL/pruning, and static checks that every element id used by
`app.js` exists in the template and that the JavaScript parses.

See [`docs/CODE-REVIEW.md`](docs/CODE-REVIEW.md) for the review notes and the
changes ranked by value.

## Known limits

- Shoutcast v1 servers that answer with an `ICY 200 OK` status line are not
  supported by Python's `http.client`; use an HTTP/Icecast mount instead.
- HLS (`.m3u8`) stations are proxied but only Safari can play them in a
  `<audio>` element.
- The first cold load of `/api/latest` waits for five RSS feeds plus the
  Archive; later loads are served from the on-disk TTL cache.
- No authentication. This is a LAN app: do not publish port 8097 directly.

## Layout

```
app.py              Flask routes + hardened stream proxy
core/               providers shared with the Kodi add-on, plus a Kodi shim
static/             app.css, app.js, artwork
templates/          index.html
test/               offline pytest suite
docs/               code review notes
feeds.json          podcast feeds
filters.json        quick filters
Dockerfile          python:3.12-slim + gunicorn
```
