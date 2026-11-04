# RaveTapePacks source-blocked handling — 1.6.47

RaveTapePacks sometimes blocks a client IP for hotlink/range requests, which
reached the browser as an opaque `<audio>` failure. 1.6.47 adds a specific,
recoverable path without changing any other source.

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
- On `source_blocked`: show the exact message, wait **5 seconds**, retry once
  silently. If that fails, show the **Check again** button in the player; each
  press performs one manual attempt. There is no automatic loop after the
  single retry; stale probes/selection changes abort.
- Success clears the banner/button and starts the main player. Other failures
  keep the generic "Stream not started — tap play to retry" behaviour.
- The play button also acts as a manual attempt while the blocked state is
  present.

Tests: backend mapping for RTP 403/timeout + generic other hosts
(`test/test_stream.py`), static client contract (`test/test_source_blocked_ui.py`),
and browser 403→200 auto-retry plus always-403 manual “Check again”
(`test_e2e/test_smoke.py`).
