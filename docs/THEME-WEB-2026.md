# Rave Player web — 1.6.15 brutalist/blung theme pass

**Status:** implemented on `feature/1.6.15` (off `main @ 3811d1a`, web 1.6.15).
**Direction:** align the web surface with the Kodi **THEME 2026** pack
(`../raveplayer/docs/THEME-2026.md`) — brutalist, modern streaming-app energy;
bling what exists, don't redesign it. The logo, wordmark and character are
untouched.

## What changed

| Surface | Change |
|---|---|
| Rail | 9 grouped sticker buttons (Downloads joined in 1.6.16) instead of 21 flat rows (fixes the 1440×900 overflow that hid Favourites/Search). Each button: icon, Impact label, `RP-0x` catalogue code (`aria-hidden`), acid active border + magenta hard shadow. Multi-section groups open a server hub (`/api/rail/<id>`). |
| Back button | 48 px paper sticker, 2 px border, magenta hard shadow, mono label; `position: sticky` under the masthead so it stays visible while scrolling (phone: clears the 58 px rail). |
| Surprise Me | Magenta sticker in the topbar (`#surprise`), backed by `GET /api/surprise` (random tape pack or show, opens the item). |
| Typography | Display = `Impact, Haettenschweiler, "Arial Narrow Bold", …` (already the token; now used for rail labels and the wordmark remains as-is). UI = system sans; catalogue/mono = system mono. |
| Texture | Halftone dots + 45° hatch (existing) plus a scanline layer on `.halftone`; hard offset shadows replace soft ones on rail/back/rows. |
| Chromium | Brand mark, `RAVE PLAYER` wordmark, `ARCHIVE · 88 → NOW` tag and the character artwork are unchanged (test guard in `test/test_nav.py`). |

## Tokens (mirrored from `docs/THEME-2026.md` §2.1)

| Token | Hex | Web use |
|---|---|---|
| ink | `#07070b` | page base, glyph plates |
| ink-2 | `#0b0b11` | active rail plate |
| paper | `#f2eee3` | body text, back sticker |
| muted | `#a8a399` | secondary text |
| acid | `#e8ff00` | Latest/Curated Gems, focus, active rail border |
| magenta | `#ff2d8f` | Surprise Me, Favourites, back shadow, brand shadow |
| cyan | `#22e6ff` | hover shadow, brand `PLAYER` shadow |
| orange | `#ff6a1a` | energy-source sublines (player status) |

All are declared in `static/app.css :root` and asserted against the Kodi pack
values in `test/test_nav.py::test_theme_tokens_match_the_kodi_2026_pack`.

## Files

- `static/app.css` — token block, rail, `.back`, `.surprise-btn`, halftone
  scanlines, row hover.
- `templates/index.html` — Surprise button; back button moved out of the title
  row so it can stick.
- `static/app.js` — grouped rail rendering + group-aware highlight, Surprise
  handler (`textContent`/`createElement` only), catalogue code as an
  `aria-hidden` child.
- `app.py` — `SECTION_PATHS`, `RAIL`, `/api/menu.rail`, `/api/rail/<id>`,
  `/api/surprise`.
- `test/test_nav.py`, `test_e2e/test_smoke.py` — coverage + screenshots.

## Screenshots

`rave-player-space/reports/web-1.6.15/` (generated with
`RAVEWEB_ARTIFACTS_DIR`, not committed): 1440×900 rail, pinned back after
scroll, Surprise result, phone rail/back.

## Guardrails

- No `innerHTML`; DOM built with `createElement`/`textContent` (tested).
- CSP unchanged — SoundCloud/Mixcloud widget hosts, same-origin stream media;
  Surprise/rail only navigate internal `/api/*` paths.
- Logo/wordmark/character untouched (tested).
- Back/rail/surprise are real buttons with ≥44 px hit areas and labels.

## Spinner (1.6.17)

The Kodi 3.9.19 spinning-head frames (10 × 128/256 px, verbatim) are a CSS
`steps(10)` sprite used by the download progress panel, the loading pill and the
Surprise reveal; see [`SPINNER.md`](SPINNER.md). Reduced motion disables the
spin/bar/stamp animation and skips the reveal wait.
