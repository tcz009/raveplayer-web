# iPhone Safari + AirPlay notes

Scope: `templates/index.html`, `static/app.js`, `static/app.css`, tests and this
document. The app is served as plain HTTP on a private LAN address
(`http://192.0.2.10:8097`) and used on an iPhone, sometimes on an iPad or a
desktop Safari. No `app.py`, `core/**`, artwork or deployment files were
changed.

There is no browser, simulator or iPhone in this environment. Everything below
is reasoned from WebKit's implementation and Apple's documented API surface,
plus the offline test suite. The last section is the exact set of things to
confirm on the device.

## What the WebKit API actually promises

Safari's AirPlay hooks are non-standard WebKit extensions:

| Surface | Behaviour |
|---|---|
| `x-webkit-airplay="allow"` / `"deny"` | Apple's documented opt-in/out for AirPlay. On iOS, WebKit maps `deny` to "wireless playback disabled"; `allow` explicitly keeps it enabled. Audio is AirPlay-capable by default, but the attribute is the documented, explicit opt-in and is kept. |
| `audio.webkitShowPlaybackTargetPicker()` | Shows the system route picker. On iOS it is gated by `RequireUserGestureToShowPlaybackTargetPicker`, so it **must** be called synchronously from a click/tap handler. On macOS WebKit additionally returns early until the element has metadata (`readyState >= HAVE_METADATA`). |
| `webkitplaybacktargetavailabilitychanged` | Fires with `event.availability === "available"` or `"not-available"`. Adding the listener enqueues an initial event; another is enqueued when the media player is created, and then whenever route availability changes. It is about *routes*, not about whether this element is currently routed. |
| `webkitCurrentPlaybackTargetIsWireless` | The honest "is this element playing to the wireless target" boolean. Changes fire `webkitcurrentplaybacktargetiswirelesschanged`. |
| Remote Playback API (`audio.remote`) | Chromium's Cast equivalent. `watchAvailability()` gives availability; `prompt()` must be called from a gesture; `remote.state` is `"disconnected"`, `"connecting"` or `"connected"`. |

Reference points used: Apple's *Opting Into or Out of AirPlay* and *Adding an
AirPlay button to your Safari media controls*, and WebKit's
`HTMLMediaElement.cpp` / `MediaElementSession.cpp` (the gesture restriction,
the availability listener enqueueing, and `webkitCurrentPlaybackTargetIsWireless`
are all visible there).

On reliability: the availability event is not a one-shot "a target exists"
confirmation before playback. It is guaranteed to be enqueued when the listener
is added and again when the media player is created, and it then tracks route
changes. The implementation therefore registers early, refreshes after a track
starts, and treats *no event yet* as unknown (the picker can still open) rather
than as "no devices". An explicit "not-available" is the only state that dims
the button; waking the Apple TV normally re-fires the event.

### What was wrong with the old implementation

1. The button was shown as soon as `webkitShowPlaybackTargetPicker` existed,
   before WebKit had reported any routes. That is a guess, and it meant the
   button could sit there with no target and do nothing.
2. Only `webkitplaybacktargetavailabilitychanged` was handled. Nothing read
   `webkitCurrentPlaybackTargetIsWireless`, so the UI could never tell the owner
   that audio was actually on the Apple TV — or that it had dropped back.
3. No `webkitcurrentplaybacktargetiswirelesschanged` listener existed, so a
   route change (including the TV going away) was invisible.
4. Chromium's `remote` was treated as "always available" and only connect /
   disconnect were listened for; `watchAvailability()`/`remote.state` were
   ignored.
5. Nothing acknowledged that the **receiver fetches the stream**. Opening the
   page at `localhost` or over Tailscale produces an AirPlay selection that
   cannot play, with no explanation.
6. The button had no disabled/unavailable treatment and no `aria-pressed`, so
   the connected state was not exposed.

## What changed

### AirPlay

- `setupRemotePlayback()` runs once at boot (not only after a track is chosen),
  so the initial availability event is caught.
- The WebKit path now listens for both
  `webkitplaybacktargetavailabilitychanged` and
  `webkitcurrentplaybacktargetiswirelesschanged`, and reads
  `webkitCurrentPlaybackTargetIsWireless` as the source of truth. The button is
  cyan and `aria-pressed="true"` only while that boolean is true.
- The button is hidden until a track is loaded (`state.current` + a `src`).
  When WebKit says `not-available`, it is shown dimmed with
  `aria-disabled="true"` and a tap explains "No Apple TV or HomePod found"
  instead of doing nothing. When availability has not been reported yet (older
  WebKit), the tap opens the picker and lets the system report reality.
- `onAirPlayClick()` is synchronous for the actual picker call, so the iOS
  gesture requirement holds. On non-iOS WebKit it refuses to open the picker
  while `readyState < HAVE_METADATA` and says the track is still loading.
- The Chromium path uses `watchAvailability()` when available,
  `remote.prompt()` in the click handler, and `remote.state` + connect /
  disconnect events. It is still only a fallback; Safari never depends on it.
- A non-LAN origin check produces a specific warning, before the picker opens,
  for `localhost`/`127.0.0.1` and for `100.64.0.0/10` / `*.ts.net` (Tailscale).
  A loopback page is warned that the TV cannot fetch from the phone's
  localhost; a Tailscale page is warned that the TV must also be on the tailnet.
  It does not block the picker, because a TV can legitimately be on the tailnet.
- If the audio element errors while the wireless target is active, the player
  says the AirPlay device could not load the stream and tells the owner it must
  be able to reach this server (same Wi-Fi, no client isolation).

### Playback and Media Session

- The direct-play path (`item.play`) calls `audio.play()` synchronously inside
  the row click; that is the first-play user gesture iOS requires.
- The `play_via` path (live-now rows and DNB mixes) awaits a resolve request,
  then plays. WebKit's transient activation lasts about 5 seconds
  (`defaultTransientActivationDuration`), so a normal LAN resolve is still
  treated as gesture-started. If a slow resolve exceeds that, the promise
  rejects with `NotAllowedError` and the player deliberately says "Tap play to
  start audio" — the player is already open and the next tap is a real gesture.
- Fixed a real pre-existing bug: `/api/filter/<key>/live` returns
  `{title, items}`, not `{url}`, so the "▶ Kool FM — live now" row always failed
  with "Could not resolve that stream". The resolver now falls back to the
  first playable station in `items`.
- Media Session handlers are split from metadata: `play`, `pause`, `stop`,
  `previoustrack`, `nexttrack` are registered on every track, and
  `seekbackward`/`seekforward`/`seekto` are registered only once
  `audio.duration` is finite. For live radio they are cleared (`null`), so the
  lock screen does not offer skip buttons that can only pop a toast.
  `setPositionState()` is re-seeded on `loadedmetadata`/`durationchange`/
  `play`/`pause`/`seeked` (throttled on `timeupdate`), and `null` while the
  duration is unknown.
- `closePlayer()` clears metadata, playback state, position state and every
  handler, and removes the `src`, so the lock screen and AirPlay route are
  released with the player.
- `visibilitychange` never pauses. Backgrounding or locking the iPhone is
  intended to keep playing. When the page becomes visible again, the UI is
  re-synced from the element (WebKit throttles events while hidden). A
  `pageshow` from the back/forward cache does the same. `pagehide` explicitly
  does **not** pause, because iOS also fires it when backgrounding into the
  back/forward cache; an actual unload tears the element down.
- There is no volume slider and no code writes `audio.volume`. iOS ignores it
  (WebKit locks the volume on small-screen iOS), so a slider would be a dead
  control. If one is ever added it must be hidden on iOS; the `IS_IOS` constant
  is there for that.

### iPhone layout and gestures

- Safe-area insets are applied to the whole shell, not just the player:
  topbar (top/left/right), rail (left/right/bottom), stage (left/right), player
  (left/right/bottom), queue panel (left/right), toast, help dialog and the
  skip link.
- `body` and the stage use `100dvh` with `-webkit-fill-available` and `100vh`
  fallbacks, so the URL bar collapsing cannot leave a 100vh gap.
- The fixed player height is **measured** (`ResizeObserver` +
  `visualViewport.resize` + orientation change) and written to `--player` on
  `body`. Previously mobile used a hard-coded `150px`, which was smaller than
  the three-row player plus the home-bar inset, so the end of the list hid
  behind the player. The queue panel, toast and stage padding all use the same
  measured value.
- Queue list is a proper flex child (`min-height: 0`, `flex: 1 1 auto`) with
  momentum scrolling, and the current item is scrolled into view when the panel
  opens. Its max height is clamped to the space between topbar and player.
- The help card is clamped to the dynamic viewport (minus safe areas) and
  scrolls; the overlay itself can also scroll. On touch devices it shows a
  touch-specific paragraph and the "Keyboard" heading, instead of assuming a
  keyboard exists.
- Coarse-pointer touch targets are at least 44 px (row actions, transport,
  rail, queue rows, back, search, help, dialog close). The mobile rail was
  grown from 54 to 58 px so the 44 px buttons are not clipped.
- `touch-action: manipulation` is applied to buttons/links/rows (no double-tap
  zoom delay; pinch-zoom still works), sticky `:hover` lift is disabled on
  touch, and pull-to-refresh is suppressed with `overscroll-behavior-y: none`
  on the root so a downward swipe cannot reload the SPA and kill playback.
- Scroll position per history entry is saved and restored (`history.scrollRestoration
  = "manual"`). Back from a tape pack returns to where you were in the list
  instead of jumping to the top every time.
- The seek slider only paints while dragging and seeks on release (`change`,
  plus a 250 ms fallback), so iOS is not asked for a Range request per pixel
  and `timeupdate` cannot fight the thumb. A live stream shows `LIVE` and
  disables the slider.
- "Load more" appends rows instead of re-rendering the whole list, and
  `paintNowPlaying()` only touches rows whose playing/paused state actually
  changed. Rebuilding an SVG on hundreds of rows on every pause was the main
  avoidable layout/CPU cost on a phone.

## What AirPlay does on this deployment

1. iPhone Safari opens `http://192.0.2.10:8097` over the LAN. The AirPlay
   picker and the Media Session API do **not** require HTTPS; plain HTTP on a
   private IP is fine for this. (There is no mixed-content problem because the
   audio URL is same-origin.)
2. When a track is selected, the element gets
   `/api/stream?u=…` and the AirPlay button appears. WebKit reports availability
   based on detected routes; with an Apple TV/HomePod awake on the same network
   it becomes enabled. If none is detected it stays dimmed and a tap explains
   why.
3. Tapping it calls `webkitShowPlaybackTargetPicker()` inside the tap. Choosing
   the Apple TV hands the media URL to the receiver. **The Apple TV fetches
   `http://192.0.2.10:8097/api/stream?...` itself**, not through the phone.
   That is why the LAN address (not localhost/Tailscale) matters. The proxy is
   Range-capable, so seeking works on the receiver.
4. `webkitCurrentPlaybackTargetIsWireless` becomes true, the icon turns cyan,
   `aria-pressed` becomes true, and "Playing on AirPlay" is shown. If the route
   drops, the event fires, the icon returns to normal, and "AirPlay
   disconnected — back on this device" is shown.
5. The iPhone keeps showing the track/queue. Locking the phone or switching
   apps keeps the audio going through the receiver; the lock screen shows the
   title/artist (and artwork when iOS chooses to fetch it) and play/pause (plus
   skip for finite tape packs, not for live radio). AirPlay volume is controlled
   by the receiver / hardware buttons.

### Failure modes the UI now explains

- **No route**: dimmed button + "No Apple TV or HomePod found…" toast. The
  system picker is not opened just to show an empty list.
- **Page opened at `localhost` / `127.0.0.1`**: warning that the TV cannot fetch
  from the phone's localhost; use the LAN address.
- **Page opened over Tailscale (`100.64/10`, `*.ts.net`)**: warning that the TV
  must be on the tailnet too, otherwise use the LAN address.
- **Apple TV on a guest VLAN / AP isolation / different subnet**: the receiver
  cannot reach port 8097. The player reports that the AirPlay device could not
  load the stream and points at same-Wi-Fi/client isolation. This cannot be
  detected from JavaScript; it is a network condition.
- **Server firewall / Docker binding**: the container must publish 8097 on an
  interface the TV can reach (the documented `-p 8097:8097` does).
- **Local (private-IP) radio upstreams**: `/api/stream` refuses private upstream
  hosts unless `RAVEWEB_ALLOW_PRIVATE=1`; that can look like an AirPlay failure
  but is the SSRF guard, and applies to local playback too.
- **Route reset when changing tracks**: some WebKit versions reset the AirPlay
  route when an element's `src` changes. If a new track suddenly plays on the
  phone, the icon honestly shows disconnected; tap AirPlay again. This is
  WebKit behaviour, not something the page can force.
- **HLS**: the proxy does not rewrite relative playlist segment URLs, so an
  HLS station that works natively may not work through the proxy on any
  receiver. Direct MP3/radio streams are the supported path (unchanged).

## What could not be verified without a device

- The actual route-picker sheet, AirPlay connection times and the exact
  availability-event ordering on the owner's iOS version. The code tolerates
  `not-available`, no event yet, and repeated events.
- Whether the route survives a track change on the current iOS/Apple TV
  firmware.
- Lock-screen artwork over plain HTTP (iOS may choose not to fetch remote
  artwork from an insecure origin; title/artist still come from the Media
  Session metadata).
- AirPlay 2 buffered audio vs URL fetch for every receiver model; the design
  assumes the documented URL-fetch path, which is what Safari does for media
  elements.
- Chromium Cast hardware (only the spec/Remote Playback behaviour is coded).
- The iOS silent/ring switch. Web Audio is muted by it; HTML media behaviour
  has varied by iOS version. If the owner hears nothing on the phone but the
  progress bar moves, check the ring/silent switch and the receiver volume
  first.
- The help dialog and queue panel on a real 320 px-wide screen; the layout is
  conservative (scrollable, 44 px targets) but was not rendered.

## Device checklist

1. On the iPhone, open `http://192.0.2.10:8097` (not a Tailscale or
   localhost URL) while the Apple TV/HomePod is awake on the same Wi-Fi.
2. Tap a tape pack track. The player appears; the AirPlay icon is in the
   transport row.
3. Tap the AirPlay icon. If it is dimmed, the toast says no device was found —
   wake the TV / check the Wi-Fi. If it is enabled, choose the Apple TV.
4. Confirm the icon turns cyan, the toast says "Playing on AirPlay", and sound
   comes from the TV. Lock the iPhone: sound should continue and the lock
   screen should show the track; play/pause should work.
5. On a tape pack, drag the seek bar: it should follow the finger and jump only
   on release; the lock screen should show a timeline and skip buttons. On live
   radio there should be no skip buttons and the player shows `LIVE`.
6. Rotate to landscape and back, and scroll to the very bottom: the last row
   and the Load more button must clear the player and the home bar.
7. Open the queue with a long list and scroll it; open Help (`?` button) in
   landscape: both must scroll and the Close button must be reachable.
8. Pull down at the top of a list: the page should not reload (pull-to-refresh
   suppressed).
9. Go into a pack, scroll, tap Back: the list should return to the previous
   scroll position, not the top.
10. Close the player with the X: the AirPlay route and lock-screen entry should
    disappear; audio stops.

## Verification run here

```
./.venv/bin/python -m pytest test -q
164 passed

node --check static/app.js
(ok)
```

Live smoke test: gunicorn on `127.0.0.1:8100`, `GET /` and `GET /static/app.js`
checked, then the server was stopped. New tests cover the audio element's iOS
attributes, the AirPlay button's hidden/aria state, the WebKit state markers,
the non-LAN warning, absence of a volume control, seek-on-release, safe-area /
dynamic-viewport CSS, and the touch help panel.
