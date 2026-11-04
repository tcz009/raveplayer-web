# RaveTapePacks source-blocked handling — 1.6.50

RaveTapePacks sometimes blocks a client IP for hotlink/range requests, which
reached the browser as an opaque `<audio>` failure. 1.6.50 keeps the
structured server code but makes the web client silent like Kodi, without
changing any other source. The per-host VPN egress added in 1.6.49 remains
active; this path is only for genuinely unreachable cases.

## Server

For a `ravetapepacks.com` target (including the existing one-directory-Referer
retry), an upstream **401/403** or **timeout** now returns:

```json
{"error":"RaveTapePacks is temporarily blocking this connection — try again in a few minutes or reboot your router.",
 "code":"source_blocked"}
```

Status is still 403/504 (or 403 after the Referer retry). 401/403/timeout for
every other source keeps the existing generic payload without `code`.

## Client

- RaveTapePacks URLs are probed through the same-origin `/api/stream` proxy
  before `<audio>` gets a src, so the JSON code is readable (a media element
  cannot see it).
- On `source_blocked` (or an RTP probe network failure), the client shows **no
  banner, toast or “Check again” action**. The rows and source links remain;
  the player is left idle (no source, paused) with an empty status line.
- One background retry is scheduled after **5 seconds**. There is no automatic
  loop after it; selecting another row aborts the pending probe. The only
  visible/debug trace is `debugEvent("playback", {action:"source_blocked", …})`
  in the opt-in debug log.
- Success after the retry starts the main player normally. Other sources keep
  the existing generic “Stream not started — tap play to retry” path.
- The per-host VPN egress (1.6.49) is unchanged; this client path does not
  disable or bypass it.


Tests: backend mapping for RTP 403/timeout + generic other hosts
(`test/test_stream.py`), static client contract (`test/test_source_blocked_ui.py`),
and browser 403→200 silent auto-retry plus always-403 silence/no-loop
(`test_e2e/test_smoke.py`).
