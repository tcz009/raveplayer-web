# Single-audio-source policy — 1.6.45

The app has two playback surfaces: the main `<audio>` element and the
SoundCloud/Mixcloud widget `<iframe>`. They must never play together. Since
1.6.45 the widget is an **inline dock in the main player's bottom slot**, not a
modal take-over.

## The dock

`templates/index.html` has one `<section id="embed-panel" class="embed-dock"
role="region" aria-labelledby="embed-title" tabindex="-1">` after the player
footer. It is `position: fixed; bottom: 0` like the player, but it has no scrim,
no `aria-modal`, no focus trap and no `overflow: hidden` on `<body>`; the page
stays scrollable and the stage/rail reserve the measured dock height through
`--embed-dock-h`. Opening an embed hides the player via `closePlayer()`, so the
dock occupies the same slot rather than sitting on top of it.

The iframe keeps the same provider widget URLs (`w.soundcloud.com/player/?…`,
`www.mixcloud.com/widget/iframe/?…`). CSS caps it with a responsive
`height: clamp(156px, 24vh, 210px)`; at 390 px it is
`clamp(150px, 27vh, 190px)` with a wrapped head and a 44 px Close target.

## Single-audio enforcement

- **Embed selected** (`openEmbed`): `closePlayer()` pauses and clears the main
  audio source, queue/progress, player chrome, MediaSession metadata/state and
  playback heartbeat **before** the iframe document is created. This is
  unconditional.
- **Embed load** (`markEmbedLoaded`): the rule is re-asserted for an autoplay
  race, but a stale load from a previous `src` is ignored so it cannot pause a
  newly started track.
- **Main player starts** (`playItem`, `resolveAndPlay`, `togglePlay`,
  `retryPlayback`, plus the `audio` "play" listener as defence in depth):
  `closeEmbed()` runs first. The old iframe element is removed from the DOM and
  replaced with a fresh blank one; that discards its browsing context, which is
  the only reliable cross-origin way to stop its audio.
- **Navigation** (`navigate`): closes the dock, so the destination starts with a
  blank iframe and a clean main player.
- **Close/Escape/Back**: `closeEmbed({restoreFocus:true})` replaces the iframe,
  hides the dock, removes `has-embed` and returns focus to the search box, row
  action or row that opened it. Focus inside the dock returns to `<main>` when
  the dock closes programmatically.

## Provider widget events

`static/embed.js` is a small bridge for the two providers' postMessage APIs.
The app keeps `script-src 'self'`, so neither vendor API script
(`w.soundcloud.com/player/api.js`, `widget.mixcloud.com/media/js/widgetApi.js`)
can be loaded. The widgets still speak the same protocols:

| Provider | Widget sends | Parent sends |
|---|---|---|
| SoundCloud | `{"method":"ready"}`, then `play`/`pause`/`finish`/`error` | `{"method":"addEventListener","value":"play"}` etc. |
| Mixcloud | `{"mixcloud":"playerWidget","type":"ready"}`, `api`, then `event` with `eventName` `play`/`pause`/`ended`/`error` | `{"type":"getApi"}` |

The bridge accepts a message only when `event.source === frame.contentWindow`
**and** the origin is an exact allowed provider origin. It posts to each
allowed origin explicitly (never `"*"`): SoundCloud `https://w.soundcloud.com`;
Mixcloud both `https://www.mixcloud.com` and `https://player-widget.mixcloud.com`
(the widget 301s to the latter). A widget `play` event is a second defence
line: if any main source survived a future code path, it is cleared before the
widget is audible.

## Cross-origin limits (deliberate)

- The parent can never read the iframe DOM, its media element, cookies or
  autoplay decisions. Play/pause state is only what the widget reports.
- The provider protocol is not a stability contract. If it changes, event
  detection fails soft; the single-audio rule still holds at every
  open/close/play boundary.
- Keyboard events inside the iframe do not reach this document. `Escape` closes
  the dock while focus is in the page; while focus is inside the widget, use the
  always-visible Close button.
- Loading the vendor API scripts would require widening `script-src` to the
  provider hosts. That was rejected: `frame-src` stays exactly
  `https://www.mixcloud.com https://player-widget.mixcloud.com
  https://w.soundcloud.com` and every other CSP directive is unchanged.
- `externalEmbed()` still accepts any `soundcloud.com`/`mixcloud.com` URL
  (pre-existing behaviour); the widget itself validates the resource.

## Accessibility

`role="region"` (not dialog), `aria-labelledby` the heading, focus moves to the
heading on open, and Close/Back/Escape return focus to the opener. The live
status (`aria-live="polite"`) announces widget play/pause/ended. The
`prefers-reduced-motion` rule disables the dock's only animation, the shared
transmission line. Close is 44 × 44 on a phone and every control keeps the
existing `:focus-visible` ring.

## Tests

- `test/test_embed_dock.py` (offline) pins the region/focus markup, the
  no-backdrop/no-scroll-lock CSS, the 620 px rules, both single-audio
  directions and the bridge source/origin checks.
- `test/js/embed_spec.js` (Node) exercises the real `static/embed.js` grammar,
  origin allow-list, handshakes and destroy path.
- `test_e2e/test_smoke.py` covers main→embed→main, navigation, close/Escape,
  widget play/pause events, no modal role, and layout at 390 × 844 and
  1280 × 900.
