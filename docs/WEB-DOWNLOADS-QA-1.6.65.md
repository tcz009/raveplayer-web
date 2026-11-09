# Web downloads: QA 1.6.62 P1/P2 fixes (1.6.65)

**Branch:** `feat/download-fixes` (from `6eb01db`, 1.6.64)
**Report:** `docs/WEB-SOURCES-QA-1.6.62.md` in the workspace (QA-01, QA-02, QA-03,
QA-04, QA-05, §8)
**Scope:** web app only. No deploy, no push, no Kodi changes; tests are offline.

## Fixed

### P1 — QA-01: truncated downloads were reported as saved

`app._download_chunks` now treats an upstream `Content-Length` as an exact
contract:

- while reading, a body longer than the declared length fails with 502;
- at EOF, `total != expected` fails with
  `DownloadError("upstream closed early — download incomplete", 502)`;
- `http.client.IncompleteRead` maps to the same error instead of being folded
  into "could not save";
- on any failure or cancellation the function closes and removes the temp file
  itself (the callers' discard remains as a second line of defence);
- no `Content-Length` (chunked upstream) keeps the previous behaviour.

Because the foreground, background-job and plain (non-NDJSON) paths all consume
`_download_chunks`, all three get the check. A job with a short body now ends
`failed`, never `completed`, and the item is not registered.

`GET /api/downloads/<id>/stream` and `/file` now compare the on-disk byte size
with the size recorded in the index. A mismatch (for example a file saved by a
pre-fix build after an early EOF) returns `409` with
`code:"download_incomplete"` instead of serving a truncated file as complete.

### P1 — QA-02: cancel was a per-worker no-op

The in-process `_DOWNLOAD_CANCELS` set is gone. Cancellation now lives in the
same WAL SQLite database as sessions and background jobs:

- table `download_cancels` (`core/download_cancels.py`, schema in
  `core/audit.py`);
- `POST /api/downloads` with `Accept: application/x-ndjson` records the
  `client_id` as `active` before the response starts;
- `POST /api/downloads/cancel/<client_id>` writes a durable `cancelled` flag and
  returns `200 {"cancelled": true, "state": "cancelled"}`;
- the streaming worker polls the shared row every 0.2 s, so a cancel handled by
  the other gunicorn worker is still seen;
- an unknown id returns `404`; a completed/failed transfer returns `409`;
- a duplicate cancel for an already-cancelled transfer is idempotent (`200`);
- before the finished temp file is registered, the worker atomically claims
  completion. If a cancel won the race, it raises `DownloadCancelled` instead,
  removes the temp file and emits `{"state":"cancelled"}`. A cancelled save can
  no longer appear in Downloads.

Rows expire after `DONE_TTL` (1 h) or `ACTIVE_TTL` (6 h for an abandoned active
row). The background-job cancel path already used the shared `download_jobs`
table and is unchanged.

### P2 — QA-05: Every Pack accepted `page=99999` as an empty room

New `_archive_search_page()` helper (used by `/api/every-pack`,
`/api/packs/<key>`, `/api/filter/<key>`, `/api/year/<year>` and `/api/search`):

1. fetch the requested page;
2. if Archive reports a total, clamp to the real last page and refetch when the
   requested page is past it;
3. if Archive returned `total:0`/no docs for a page > 1, learn the total from
   page 1 and fetch the clamped last page;
4. if the last page is empty although the total is non-zero, retry once; a
   persistent empty page is a `502` (`code:"source_unavailable"`), never an
   empty room.

`page=99999` is still capped at `MAX_PAGE` (500) by `_page_arg`; the helper then
clamps 500 down to the real last page. Tests cover Every Pack, an Archive
section, a year, a one-shot empty-last-page flake and a persistent flake.

### P2 — QA-01/§8: RaveTapePacks download affordance hidden

`RAVEWEB_RTP_DOWNLOADS` defaults to `0`. While off:

- `_download_allowed("ravetapepacks", …)` returns a 400 reason, so
  `POST /api/downloads` and `/api/downloads/jobs` reject the source;
- `_mark_download` therefore removes `item.download` from RTP post/pack rows;
- playback (`play`), Range streaming and favourites are untouched.

Re-enable after verifying a full live save:

```sh
RAVEWEB_RTP_DOWNLOADS=1
```

## Investigation — QA-03 / QA-04 (no code change)

### Mikus large Drive download 504 after ~90 s

The 90 s figure is the app's read timeout, not the 1800 s download deadline or
the gunicorn 120 s worker timeout:

- `DOWNLOAD_DEADLINE` is 1800 s;
- `_open_stream` sets the upstream socket timeout to
  `READ_TIMEOUT = 30 s` (`app.py`);
- `_download_chunks` maps `socket.timeout` during `resp.read()` to
  `DownloadError("the download timed out", 504)`.

The report's 301 progress ticks over roughly 60 s followed by a 30 s stall gives
the observed 90.5 s 504. In other words Google Drive stopped sending data for
30 s; the proxy correctly reported a timeout. The small 11 MB file from the
same source completed, and the 134 MB file plays through
`/api/mikus/drive/<id>/stream`, so the resolver and URL were valid. This is
consistent with Drive-side throttling/connection stalling, not a bad link or a
too-short app deadline.

### RaveTapePacks playback truncation

`_serve_stream.generate()` reads `resp.read1(64 KiB)` and ends the response on
`b""`, `socket.timeout`, `HTTPException` or `OSError`. It deliberately does not
forward `Content-Length` (a byte cap or deadline can stop early), so a full-file
200 can end cleanly at an arbitrary offset and the browser simply stops. Range
requests are answered correctly; the flakiness is in the RTP origin/egress
cutting long-lived full-body responses. The download path now fails loudly
instead of saving the fragment, and downloads are hidden for RTP (above).

A bounded retry/resume was considered but not added in this change:

- the safe form is a `Range: bytes=<n>-` resume, which needs a second
  `_open_stream` lifecycle and a strict `206`/`Content-Range` check;
- for a 200-only origin the only fallback is a full restart, which doubles a
  potentially 100 MB transfer;
- the current fix surfaces the truncation instead of masking it, and RTP
  downloads are disabled, so the residual risk is playback only.

If a future change adds resume, it must keep the final `total == expected`
check; a retry must never convert an incomplete body into `saved`/`completed`.

## Tests

New/updated offline tests:

- `test/test_download_integrity.py`
  - short body with `Content-Length` → 502 + no file;
  - overlong body → 502 + no file;
  - mid-stream `IncompleteRead` → 502 + no file;
  - no `Content-Length` → unchanged, saves the exact bytes;
  - foreground NDJSON and plain paths report the error and register nothing;
  - background job ends `failed` with no library item;
  - a saved copy truncated on disk is not served (409 on stream/file);
  - two independent OS processes sharing one data dir: worker A streams,
    worker B cancels through the HTTP endpoint, A emits `cancelled`, no file is
    registered and no temp file is left;
  - unknown cancel id → 404;
  - RTP marker hidden by default, playback still present, API rejected, and
    re-enabled with `RTP_DOWNLOADS_ENABLED=True`.
- `test/test_spinner.py`: cancellation now goes through the shared store;
  unknown/finished/invalid cancel ids covered.
- `test/test_pagination_web.py`: Every Pack / Archive section / year clamp to
  the last page; empty known last page retried once; persistent empty page 502.
- `test/test_api.py`: filter clamp expectation updated to the real last page.

Run: `pytest test -q` (offline). The full suite was run twice before commit:
**858 passed** each time (115 s and 151 s).

## Residual

- RTP downloads stay off until a live verification; set
  `RAVEWEB_RTP_DOWNLOADS=1` only then.
- No resume/retry was added for Drive/RTP truncation; failures are explicit
  502s. A 30 s Drive stall still fails a large save after one attempt.
- Pre-existing truncated library items are not repaired automatically; the
  stream/file endpoints now return 409 so the user can delete and re-download.
- The browser panel's cancel toast still says "Download cancelled" even when
  the API answers 404/409 (the request is best-effort). The server contract is
  now correct; a UI copy tweak is a separate change.
