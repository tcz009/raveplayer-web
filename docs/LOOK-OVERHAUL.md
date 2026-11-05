# Look overhaul — web parity with the 2026 sticker language

**Branch:** `feat/look-overhaul` (base `main` 1.6.51) · **Scope:** visual/UX
only · **No deploy.**

The Kodi Rave Screen design spec
(`../raveplayer/docs/rave-screen/RAVE-SCREEN-DESIGN.md`, sibling checkout)
pins ink `#07070b`, paper `#f2eee3`, muted `#a8a399`, acid `#e8ff00`,
magenta `#ff2d8f`, cyan `#22e6ff`, one spot per surface, hard offset shadows,
Impact display and mono print codes. The web already used those colours; this
pass adds the missing **grid, focus furniture, fact column and printed
statuses**, and makes the paper theme answer every new element.

**Unchanged:** every API route, the section taxonomy, the inline embed dock
markup/JS, the single-audio policy, the service worker, the PWA manifest and
the offline cache. No new font, image direction or logo change.

---

## 1. What changed

| Area | Before | After |
|---|---|---|
| Grid | ad-hoc 52/14/6 px values | `--space-1…12` (4/8/12/16/24/32/48) and `--row-h: 48px`; rows are 48 px art + 8×12 padding |
| Rail focus | 2 px acid inset | 4 px acid slab + hard 3 px magenta offset (ink on paper) |
| Row hover/focus | 1 px translate + soft shadow | 4 px acid inset bar + the same hard print shadow |
| Row facts | none | mono `.row-fact` from `item.duration`/`item.fact`, 16-character Kodi Label2 budget |
| Empty state | one line of grey text | dashed printed plate, `STANDBY` kicker, acid/magenta left rule |
| Error state | magenta panel | magenta 4 px left rule + existing message/retry |
| Focus ring | acid everywhere | one `--focus-ring`: acid on ink, ink on paper |
| Light theme | covered most surfaces | also covers rail bar, row bar/fact, empty/error plates, focus ring, state action |

All motion remains 120 ms border/transform; `prefers-reduced-motion` is
untouched. The embed dock keeps its own light/dark selectors and the
single-audio ordering (`closePlayer()` before setting the iframe src) is
untouched — `test/test_embed_dock.py` still passes in full.

## 2. Before / after

Chromium captures from `test_e2e`, same fixtures, before = `main` 1.6.51,
after = this branch.

| View | Before | After |
|---|---|---|
| Latest, dark 1280 | [before](look-overhaul/web/before/latest-dark.jpg) | [after](look-overhaul/web/after/latest-dark.jpg) |
| Latest, light 1280 | [before](look-overhaul/web/before/latest-light.jpg) | [after](look-overhaul/web/after/latest-light.jpg) |
| Phone, dark | [before](look-overhaul/web/before/phone-dark.jpg) | [after](look-overhaul/web/after/phone-dark.jpg) |
| Phone, light | [before](look-overhaul/web/before/phone-light.jpg) | [after](look-overhaul/web/after/phone-light.jpg) |
| Embed dock 1280 | [before](look-overhaul/web/before/embed-dock.jpg) | [after](look-overhaul/web/after/embed-dock.jpg) |
| Tape Packs, light | [before](look-overhaul/web/before/tape-packs-light.jpg) | [after](look-overhaul/web/after/tape-packs-light.jpg) |

The rail active state and row hover bar are the clearest deltas; the light
shots prove the paper theme is still deliberate after the new rules.

The full e2e artifact set (26 PNGs: dark/light desktop + phone, help, login,
surprise reveal, spinner, download panel, debug panel, embed dock at two
viewports) is written by `RAVEWEB_ARTIFACTS_DIR=... pytest test_e2e` under
`docs/look-overhaul/after/`. Only the review selection above is committed as
JPEG; the PNG originals stay local and are ignored by the doc links.

## 3. Assets

- The 50 large stickers in `static/art/` were 512²; the rail renders at 22 px,
  row art at 48 px and the player at 54 px. They are now 256² (Lanczos),
  **17.0 MB → 7.8 MB (−10.7 MB, −62 %)**. `icon.png` (512 PWA icon) and
  `icon-180.png` were deliberately kept.
- A re-encode audit of the remaining PNG/JPEG found no further lossless gain;
  they are already palette/optimised, and `hero.jpg`/`fanart.jpg` are an
  intentional duplicate for the Kodi shim (`scripts/make-art.py`).
- No external font, no `@font-face`, no new asset direction.

## 4. Tests

- `pytest test`: **507 passed** (500 baseline + 7 new in
  `test/test_look_overhaul.py`); the new checks pin the grid/focus tokens, the
  rail bar, the 48 px row, `row-fact` (textContent, 16 chars), the printed
  standby/error plates, light-theme parity and "no external font".
- `pytest test_e2e`: **36 passed** with Chromium
  (`RAVEWEB_REQUIRE_BROWSER=1`): embed dock 1280/390, single-audio, PWA,
  dark/light phone and the screenshot flows.
- Untouched and still green: `test_embed_dock.py`, `test_theme.py`,
  `test_pwa.py`, `test_nav.py`, `test_art_duplicates.py`.

## 5. Files

- `static/app.css` — tokens, rail, rows, facts, status plates, light overrides.
- `static/app.js` — `row-fact` creation in `row()`.
- `static/art/*.png` — 50 stickers at 256².
- `test/test_look_overhaul.py` (new).
- `docs/LOOK-OVERHAUL.md`, `docs/look-overhaul/web/` (before/after).

## 6. Limits

- These are browser captures, not device screenshots; the iPhone Safari QA in
  `docs/QA-IPHONE.md` still applies.
- Focus rings are `:focus-visible` only (keyboard); touch has no ring by design.
- The row fact is empty when the API does not publish a duration; it never
  invents one.
