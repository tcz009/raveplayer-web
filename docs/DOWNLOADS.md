# Downloads / offline + background jobs (web, 1.6.23)

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
| `POST /api/downloads/jobs` `{url,title,source}` | queue a **server-side** download; returns `202 {"id","job"}` immediately. Same eligibility/caps/host checks. An identical URL already active returns the existing job (`200`, `already`); an already-completed URL returns its Downloads id (`200`, `already`, `state:"completed"`). |
| `GET /api/downloads/jobs` | `{"jobs":[…],"concurrency":1..2}` for the signed-in identity (owner sees all users' jobs). |
| `GET /api/downloads/jobs/<id>` | `{"job":{…}}`; 404 for another friend's job. |
| `POST /api/downloads/jobs/<id>/cancel` | request cancellation; 200 `{id,state:"cancelled",job}` or 409 if already finished. Partial file discarded. |

Rows shown in the player get a Download icon only when the server attached
`download` metadata. The Downloads view rows expose Save-to-device, play
(offline copy) and delete.

## Background jobs

`POST /api/downloads/jobs` creates a persisted job and returns before any bytes
move. A daemon worker writes the same atomic `<safe-title>-<8hex>.mp3` file
under `RAVEWEB_DATA/downloads`; closing the tab does not cancel it. Jobs are
stored in `downloads/jobs.json` (atomic replace), separate from `index.json`.

States: `queued` → `downloading` → `completed` / `failed` / `cancelled`.
Each public job carries `id`, `identity`, `role`, `title`, `source`, `state`,
`bytes_done`, `bytes_total`, `progress` (0–100), `error`, timestamps and the
completed `download_id`; the URL and internal entry are never returned.

- **Concurrency** `RAVEWEB_DOWNLOAD_JOBS` (default 2, clamped 1–2); extra jobs
  wait as `queued`. Job metadata is capped at 20 live/history entries.
- **Ownership**: a job belongs to the signed-in `owner` or `friends` identity.
  Friends see only their own jobs; the owner/admin sees everyone's and may
  inspect or cancel them. The Downloads library itself remains shared.
- **Restart/failure cleanup**: on the first jobs request after a process
  restart, any `queued`/`downloading` row is marked `failed`
  (“interrupted by server restart”) and its unique `.part` file is removed.
  Failures, cancellation and storage-cap errors also discard the partial file.
- **Limits** are the phase-1 guards in the next section (per-file, total,
  free-space, item count); a job that would exceed them fails with a sanitized
  error and writes nothing.

The UI adds **Download in background** beside Cancel on the progress panel, a
topbar spinner/percent chip linking to Downloads, live queued/downloading rows
with cancel at the top of the Downloads view, persistence across full page reloads, and a
completion toast when you are elsewhere.

## Storage and guards

- Files and `index.json` live under `RAVEWEB_DATA/downloads` (Docker `/data/downloads`).
- Per file `RAVEWEB_DOWNLOAD_BYTES` (default 500 MiB), library total
  `RAVEWEB_DOWNLOAD_TOTAL` (default 2 GiB), 200 items, plus a free-space reserve.
- Background concurrency `RAVEWEB_DOWNLOAD_JOBS` (default 2, clamped 1–2).
  `Content-Length` is checked before writing; the byte counter is checked while
  writing; temp file + atomic rename; index writes are locked (thread +
  `fcntl`) and atomic.
- Filenames: sanitised title stem + short URL hash + extension —
  `<safe-title>-<8hex>.mp3` / `.m4a`. No provider URL ever reaches a filename.
- No auto-download and no preloading. Background saving starts only from an explicit `POST /api/downloads/jobs`.

## Scope / later phases

Direct-file eligibility is exactly the four sources above; background jobs use
the same predicate. Background jobs are in-process workers with persisted
metadata (no external queue/broker); a restart marks interrupted jobs failed
rather than resuming them. Multi-user accounting is only the owner/friends
identity split, not arbitrary accounts. Podcast/RSS enclosures, Legacy direct
files and GetDarker could be added later with the same predicate; they are
deliberately excluded now to match the Kodi add-on's context-menu eligibility.

## Tests

`test/test_downloads.py` (8 offline cases: eligibility, filename safety, save →
list → Range stream → attachment → delete, duplicate URL, size/total guards, no
auto-download, source markers), `test/test_download_jobs.py` (7: lifecycle,
survive-request, cancel mid-stream, concurrency queue, restart recovery,
owner/friends visibility, completion into the index) and browser e2e round
trips including background → navigate away → completes → Downloads.
