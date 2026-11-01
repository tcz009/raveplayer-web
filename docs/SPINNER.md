# Spinner pass (web, 1.6.17)

The Kodi 3.9.19 spinning-head overlay, mirrored on the web. The art is the Kodi
2026 pack — **not redrawn**: the ten `head-{128,256}-00..09.png` frames are copied
verbatim from `raveplayer/resources/art/spinner/` into
`static/art/spinner/`. Two derived sprite sheets (`sprite-128.png`,
`sprite-256.png`) tile those frames horizontally so CSS `steps(10)` can play
them; the frames themselves stay in the repo for provenance/parity.

## Art and animation

| Piece | Value |
|---|---|
| Frames | 10, Kodi originals, 128 px and 256 px |
| Sprite sheet | 10 × size wide (1280×128 / 2560×256), alpha |
| CSS | `.spinner-head` uses `background-size: calc(var(--head) * 10) var(--head)` and `animation: head-spin 1s steps(10) infinite`; `image-set()` picks the 256 sheet on 2x screens |
| Sizes | `.small` 20 px (pill), `.medium` 60 px (download panel), `.large` 104 px (reveal) |
| Reduced motion | `@media (prefers-reduced-motion: reduce)` sets `animation: none` and the first frame; the JS reveal skips its wait entirely |

## 1. Download progress panel (mirrors Kodi `SpinnerWindow`)

On an explicit Download, `POST /api/downloads` is sent with
`Accept: application/x-ndjson`. The server streams JSON lines while it proxies
the file to `/data/downloads`:

```
{"state":"started","filename":"…","title":"…","total":123456}
{"state":"progress","received":65536,"total":123456}
{"state":"saved","item":{…}}   |   {"state":"cancelled"}   |   {"state":"error",…}
```

The panel shows the spinning head, filename, lime bar, %, `X of Y` (Kodi's
`format_bytes` wording), `N sec remaining` (or `Size unknown` when the upstream
sends no `Content-Length`) and a Cancel button. Cancel POSTs
`/api/downloads/cancel/<client_id>` and aborts the response; the server's chunk
loop stops and the partial file is discarded. Without a known size the bar is
indeterminate (striped, static under reduced motion).

## 2. Global loading pill

Every `api()` helper call increments a counter; the topbar pill appears only
after **150 ms** with any request still in flight, so fast section switches never
flicker it. It clears in `finally` on success or error; `role="status"`
`aria-live="polite"`; the old READY/ITEMS status is dimmed while it is shown.

## 3. Surprise Me reveal

The `/api/surprise` fetch starts immediately. Unless reduced motion is on, the
reveal overlay spins its head fast and holds for at least 900 ms, then stamps
the chosen pack/show name (scale/rotate + hard cyan→magenta shadow, 620 ms)
before navigating. Under reduced motion the overlay is skipped and navigation
happens as soon as the fetch resolves.

## Tests

- `test/test_spinner.py` (9): frame/sheet count and PNG dimensions, CSS
  `steps(10)`/`image-set`/reduced-motion rules, markup ids, client state hooks,
  NDJSON started→progress→saved stream, cancel mid-stream (nothing saved),
  cancel-id validation.
- `test_e2e/test_smoke.py` (+4): download panel visible with progress then
  saved, cancel round trip, loading pill 150 ms grace (fast navigation shows
  none; slow fetch shows then clears), Surprise reveal spin/stamp timing, and a
  reduced-motion run (no head animation, no reveal wait, pill still announces).

Screenshots: `rave-player-space/reports/web-1.6.17/`
`07-download-panel.png`, `08-loading-pill.png`, `09-surprise-reveal.png`
(generated with `RAVEWEB_ARTIFACTS_DIR`).
