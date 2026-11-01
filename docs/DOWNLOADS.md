# Downloads / offline (web, 1.6.16 phase 1)

Explicit, direct-file saves for the web player. Nothing is fetched until the
user presses **Download** on an eligible row, and only the four curated direct
sources can ever be saved. The same rule is shared with the Kodi add-on so both
platforms offer the same rows.

## Eligibility (server-enforced, not client-trusted)

A row is downloadable only when the server marked it (`item.download.source`)
and the posted URL passes `app._download_allowed`:

| Source key | Allowed host(s) | File types |
|---|---|---|
| `archive` | `archive.org` / `*.archive.org` (item storage nodes) | `.mp3`, `.m4a` |
| `moxser` | `enceladus.feralhosting.com` / `*.feralhosting.com` | `.mp3`, `.m4a` |
| `hardscore` | `hardscore.com` / `*.hardscore.com` | `.mp3`, `.m4a` |
| `pirate` | exact `stream_url`s in `resources/pirate-radio.json` | `.mp3`, `.m4a` |

Always rejected: Mixcloud/SoundCloud/YouTube/Vimeo widget or page URLs, player
and API pages, live radio/HLS/`.m3u8`, podcast/RSS enclosures, Legacy-scraper
tracks and GetDarker rows (phase 1 scope), non-http(s) schemes, URLs with
credentials, and non-file paths. Every redirect hop is re-validated by the
stream proxy's `_open_stream` (public addresses only, DNS pinned).

## API

| Method / path | Purpose |
|---|---|
| `GET /api/downloads` | library list (`items`, `storage.used/limit/items`) |
| `POST /api/downloads` `{url,title,source}` | proxy-save one direct audio file; duplicate URLs are idempotent. With `Accept: application/x-ndjson` it streams `started`/`progress`/`saved`/`cancelled`/`error` lines for the progress panel (1.6.17) |
| `POST /api/downloads/cancel/<client_id>` | best-effort cancel of a streaming save; the partial file is discarded |
| `GET /api/downloads/<id>/stream` | offline playback from the local copy (Range, CORP `same-origin`, `nosniff`) |
| `GET /api/downloads/<id>/file` | save the local copy to a device (`Content-Disposition: attachment`, safe filename, Range) |
| `DELETE /api/downloads/<id>` | delete file + index entry |

Rows shown in the player get a Download icon only when the server attached
`download` metadata. The Downloads view rows expose Save-to-device, play
(offline copy) and delete.

## Storage and guards

- Files and `index.json` live under `RAVEWEB_DATA/downloads` (Docker `/data/downloads`).
- Per file `RAVEWEB_DOWNLOAD_BYTES` (default 500 MiB), library total
  `RAVEWEB_DOWNLOAD_TOTAL` (default 2 GiB), 200 items, plus a free-space reserve.
  `Content-Length` is checked before writing; the byte counter is checked while
  writing; temp file + atomic rename; index writes are locked (thread +
  `fcntl`) and atomic.
- Filenames: sanitised title stem + short URL hash + extension —
  `<safe-title>-<8hex>.mp3` / `.m4a`. No provider URL ever reaches a filename.
- No auto-download, no preloading, no background fetching.

## Scope / later phases

Phase 1 is exactly the four sources above. Podcast/RSS enclosures, Legacy direct
files and GetDarker could be added later with the same predicate; they are
deliberately excluded now to match the Kodi add-on's context-menu eligibility.

## Tests

`test/test_downloads.py` (8 offline cases: eligibility, filename safety, save →
list → Range stream → attachment → delete, duplicate URL, size/total guards, no
auto-download, source markers) and one browser e2e round trip.
