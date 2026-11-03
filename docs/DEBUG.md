# Debug log (opt-in) — 1.6.31

A client-side ring buffer for chasing browser-only bugs: API requests, download
jobs, routes, media events, connectivity, service-worker state and uncaught
errors, shown in a small floating **DEBUG** panel. It is **off by default** and
never sends anything anywhere.

## Turn it on

| How | Effect |
|---|---|
| Open the app with `?debug=1` | Turns the log on and stores the flag in `localStorage` (survives reloads) |
| `localStorage.setItem("raveplayer.debug", "1")` then reload | Same, without a query string |
| Open with `?debug=0` | Turns it off and removes the stored flag |
| `localStorage.removeItem("raveplayer.debug")` then reload | Off |
| `javascript:localStorage.clear()` | Nukes it with everything else |

The flag key is `raveplayer.debug`; the only accepted on values are `1` and
`true`. Query string wins over the stored value for that load.

The app must be opened normally (signed in). The debug bundle is a same-origin
script (`static/debug.js`) loaded before `static/app.js`; there is no inline
scripting and no CSP change.

## The panel

Bottom-right, above the player: **DEBUG**, a `used / 500` counter, and three
actions.

| Action | What it does |
|---|---|
| **Copy** | Copies the buffered events as newline-delimited JSON to the clipboard |
| **Download** | Saves `rave-debug-YYYYMMDD-HHMMSS.json` (metadata + all buffered events) |
| **Clear** | Empties the buffer and the rendered list; the panel stays open |

The list shows the newest 200 events for readability; the buffer and Copy/
Download always hold up to **500**. Oldest events are dropped first. Nothing is
persisted to disk by the app.

## What is recorded

| Kind | Fields |
|---|---|
| `debug` | one entry at start: buffer size, visibility, online state |
| `api` | `path`, `method`, `status`, `ms`, `requestId` (from `X-Request-ID`) — no bodies, no headers, no query strings |
| `download` | job `id`, `state` (queued/downloading/completed/cancelled/error), `bytes`, `total`; foreground downloads add `mode` and `state` |
| `route` | hash-route path (query string stripped), initial/`push` marker |
| `media` | audio `event` (play/pause/playing/waiting/ended/error/loadedmetadata/seeking/seeked/stalled), stream path, position, paused, readyState |
| `visibility` | `document.visibilityState` on change |
| `network` | `online: true/false` |
| `error` | uncaught error message/source/line, unhandled rejection message, failed resource tag/path |
| `sw` | service-worker registration, update, install/wait/active state changes, controller change, scope |

`ms` is wall-clock duration of the fetch as the browser saw it. `requestId` is
echoed by the Flask app on every response and written on the matching server
line (`req id=… method=… path=… status=… ms=…`), so a client event maps to the
server log even when several clients are active.

Only same-origin `fetch` calls are wrapped. The log does not intercept XHR, DOM
mutation, storage or cross-origin requests, and it does not change audio,
download or routing behaviour when on.

## Privacy and redaction

Everything is redacted **before** it is stored, so even the on-screen panel and
the Copy/Download output are clean:

- keys matching `password`/`passwd`, `token`, `secret`, `cookie`,
  `authorization`, `session`, `csrf`, `api_key`, `signature`, `credential`, `sid`
  become `"[redacted]"`;
- URL/query and `key=value`-shaped strings with those names are scrubbed inline;
- `Bearer …` tokens, JWTs and `raveplayer_session=…` cookie values are masked;
- strings are truncated at 300 characters; nested objects are depth/size
  limited; cycles become `[circular]`.

Requests are logged by **path only** — search terms and signed query strings
never enter the client buffer or the server log. No cookies or headers are
recorded. The panel is local-only: there is no `sendBeacon`, no XHR upload and
no third-party endpoint; Copy uses the clipboard and Download writes a local
file.

The file you download may still contain stream paths, job ids and titles of
what you played. Treat it like a screenshot of the app, not a public artifact.

## Performance

When off, `debug.js` reads the flag and stops. There are no listeners, no
fetch wrapper, no DOM and no storage writes; `app.js` holds a `null` reference
and every hook is one boolean check. When on, rendering is batched to one frame
and the buffer is hard-capped at 500 events, so it cannot grow without bound.

## Server side

- Every response carries `X-Request-ID` (generated client-side by the debug
  wrapper, otherwise by Flask).
- Incoming values must match `^[A-Za-z0-9._-]{8,64}$`; anything else is
  replaced, so a header cannot inject newlines into the log.
- The access line is `req id=… method=… path=… status=… ms=…`. Stream and
  download warnings add the same `id=` token.
- Logs stay structured `key=value` and never include query strings, cookies or
  request bodies.

## Tests

- `test/test_debug.py` — toggle resolution, redaction, the 500-event ring
  buffer and copy/format output (executed against the real `static/debug.js`
  with Node), plus the `X-Request-ID` echo/validation/log-correlation contract
  and the "one spinner" client hooks.
- `test_e2e/test_smoke.py` — the panel appears only when opted in, records
  route/API/media events, redacts before display, and Copy/Download/Clear work;
  and exactly one top-right spinner shows while a background download runs
  (the pill returns for a slow non-download API load).
