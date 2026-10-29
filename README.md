# Rave Player — web build

The Kodi add-on's content sources, served as a browser app so it can run in
Docker on the home server and be used from any device on the LAN.

Live on `server-host`: <http://192.0.2.10:8097>

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

## Why a server proxy

`/api/stream` proxies audio. Two reasons:

1. **Hotlink protection** — ravetapepacks.com rejects requests without a
   `Referer`; the browser cannot forge one, the server can.
2. **Mixed content / CORS** — radio streams are plain HTTP, and Archive
   redirects would otherwise be blocked from an HTTPS page.

Range requests are forwarded, so seeking works. Private IPs are refused by
default (SSRF guard); set `RAVEWEB_ALLOW_PRIVATE=1` to allow LAN streams.

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
```

The bind-mounted `data/` directory must be writable by uid 10001 (the
container user) — `deploy.sh` fixes that for you. Favourites failing silently
is the symptom when it is not.

## API

| Endpoint | Purpose |
|---|---|
| `/api/menu` | sections |
| `/api/latest` | merged newest |
| `/api/packs`, `/api/packs/<key>` | Archive sections |
| `/api/item/<id>` | tracks in a tape pack |
| `/api/filter/<key>` | quick filters (Kool FM included) |
| `/api/filter/<key>/live` | live stations for a filter |
| `/api/live`, `/api/live/<tag>` | radio |
| `/api/podcasts`, `/api/podcast/<id>` | RSS feeds |
| `/api/dnb`, `/api/dnb/<mix>` | DNB Radio |
| `/api/years`, `/api/year/<year>` | by-year browse |
| `/api/legacy/...` | the surviving sites |
| `/api/search?q=` | Archive search |
| `/api/favourites` (GET/POST/DELETE) | favourites |
| `/api/stream?u=&ref=` | Range-capable audio proxy |
| `/healthz` | container health |

## Layout

```
app.py              Flask routes + stream proxy
core/               providers shared with the Kodi add-on, plus a Kodi shim
static/             app.css, app.js, artwork
templates/          index.html
feeds.json          podcast feeds
filters.json        quick filters
Dockerfile          python:3.12-slim + gunicorn
```
