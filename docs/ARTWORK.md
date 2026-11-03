# Rave Player — artwork and visual system

**Concept: The Last Transmission.** Rave Player is treated as a pirate
transmitter that never went off air: a 1993 rooftop aerial still beaming a
halftone smiley over the city, with the archive stored in the things that
survived — TDK cassettes, flyers, white labels and a bedroom CRT. The screen is
a dark room; the hero is the light coming through the window, and the section
rail is a sheet of spot-colour stickers stuck to the wall. Every image is drawn
from code with Pillow, so the whole set can be rebuilt, changed and credited.

This is a *language*, not a facsimile. Nothing is scanned, traced or copied
from a flyer, sleeve, logo or photograph. The references are the era's visual
grammar — halftone screens, misregistered spot colour, crop marks, smileys,
tape inlays, tribal cut-outs, neon bolts, tuning dials, CRT glass — not a
specific piece of somebody else's design. There is no drug imagery or
paraphernalia; the acid-house moment is referenced through colour, repetition
and the smiley, not through literal objects.

---

## 1. The system at a glance

| Layer | Decision |
|---|---|
| Field | near-black `#07070b`; panels are ink, never flat grey |
| Signal | acid yellow, hot magenta, electric cyan, heat orange |
| Paper | warm off-white `#f2eee3` for all readable copy |
| Print | halftone dots, 45° hatch, crop marks, registration offsets |
| Type | condensed display caps + system UI + catalogue mono |
| Icons | one bright spot-colour sticker per strand; ink-plate glyph |
| Wordmark | acid smiley sticker + misregistered `RAVE / PLAYER` |
| Hero | rooftop mast, halftone acid sun, city and crowd, graded dark left |
| Motion | one signal scan, one level meter; both stop for reduced motion |

The design is deliberately **two-colour at heart**: every section is one spot
colour plus black ink, with paper reserved for the writing. That is how a
screen-printed sticker behaves, and it gives the rail a colour-coded shape
language while the library itself stays calm and mostly text.

---

## 2. Era references, cue by cue

### The smiley — Latest, app icon, the brand mark
The 1988–89 acid-house smiley, reduced to a circle, two eyes and a wide arc. It
is a generic face drawn from primitives, not a reproduction of any particular
club or label mark. The app icon is acid stock with a black face plate: the
**wink is also a play triangle**, so the rave face and the player are one
shape. A magenta disc sits behind it, offset like a second pass through the
screen press. The Latest tile is the same face in the rail, minus the offset.

### The mast — Kool FM, Live Radio
A cyan sticker with a black lattice transmitter and three arcs. Kool FM and the
London pirates were aerial-first operations: the tower is the station's
silhouette. The arcs are deliberately coarse so they survive at 22 px; the base
bar is hazard tape.

### The cassette — Tape Packs, Quick Filters
A warm-paper "white label" sticker with a black C90 shell. The window, reels,
label text and shell screws are knocked out of the black plate, so the paper
shows through like an unprinted tape inlay. Drawn from the object, not from a
brand: no tape manufacturer's logo or label design is used. This is the
archive's physical ancestor.

### The tower + cassette — Toronto Rave Mixtape Archive
A cyan 2026 sticker (`RP-20`) with a black CN Tower and skyline rising from a
cassette shell. The tower's antenna, saucer pod and tapered shaft stay legible
at 64 px, and the cassette label/window/reels are knocked out in the tile
colour so the shape reads as a tape and not a second tower. It is a generic
Toronto skyline and a generic cassette — no CN Tower photograph, no station or
tape-brand logo is traced. The same 256 px file is used by Kodi
(`resources/art/toronto.png`) and the web build (`static/art/toronto.png`); the
Kodi repo's `scripts/make-toronto-art.py` rebuilds both and the 64/128/256
proof sheet.

### The mask — Podcasts & Mixes (jungle)
A magenta sticker with a tall black line-plate mask: slit eyes, a diamond
forehead mark, teeth and cheek stripes. Jungle's sleeve language was dark,
ritual and silhouette-led; the mask is the counterweight to the bright smiley.
The "Podcasts & Mixes" section is present-day jungle/DnB shows, so the strand
carries over.

### The bolt — DNB Radio (hardcore)
An acid sticker with a black hardcore zigzag and knocked-out speed cuts.
Hardcore and early DnB flyers used the bolt as a pure energy mark; it is the
most readable silhouette in the set at 22 px, and the acid/black pair is the
loudest thing in the rail.

### The calendar — By Year
A cyan sticker with a black portrait ticket, binding rings and a huge
knocked-out **93**. 1987–2026 is the archive's range; 88 → NOW is the emotional
centre. The date grid is reduced to blocks so it stays a silhouette, not a
form; the cyan field is the small house/garage shimmer in the set.

### The 2-step 2 — UK Garage
A paper sticker with a forward-sheared black **2**, one paper specular arc
inside the bowl and a double rewind chevron at the left. The numeral is the
genre's name and its rhythm (two-step); the chevrons are the wheel-up, and the
warm paper stock is the chrome/white-label surface. The chrome is not a
gradient: it is one thin paper knockout, so the mark still reads as a solid 2 at
22 px and gains the gloss only at review size. `2 STEP` is a generic flyer
caption; no real flyer, label, sleeve or crew mark is used. This is a 2-step
plate, not a portrait or a record.

### The CRT — Legacy Sites
A paper sticker with a black bedroom monitor. The screen, browser chrome, text
bars and cursor are knocked out of the plate, so the paper reads as the
phosphor. Legacy sites are the 2014 survivors; the CRT is a nod to the web
before the archive, and to the fact that this music was first traded on screens
as much as on tape.

### The heart — Favourites
A magenta sticker with a fat black heart and a knocked-out glint. Favourites
are personal, not archival; the heart is the one soft shape in the rail.

### The magnifier — Search
An acid sticker with a black magnifier; the lens carries a knocked-out smiley.
Searching the archive should feel like finding the signal again, not like using
a database.

### The selector headphones — DJ EZ (Quick Filters)
A magenta sticker with a black pair of over-ear headphones. A crossfader strip
is cut into the headband and the cups are speaker grilles; headphones are the
generic DJ/selector mark and the strip is the mix. It is deliberately not a
portrait, a face or a name in a logotype. `SELECTOR` is a generic caption, not a
brand. The Garage Nation filter row can reuse `garage.png`; both filters use the
same slot as the tape-pack fallback until they are wired.

### The hero — "The Last Transmission"
A rooftop at night: a lattice mast with an acid rim, a huge halftone sun printed
slightly off-register in magenta, searchlight cones, a city skyline, a crowd
reduced to silhouettes and rim light. The left third is graded to near-black so
the page title always sits on ink. The print language is the point: the sun is
made of dots, not a glow.

### The flyer furniture
Every section tile has an ink halftone screen, a paper sheen, a 2 px inner
keyline, four crop marks and a small catalogue code (`RP·01` … `RP·20`). The
texture and codes are invisible at 22 px and legible at 52 px and above — the
detail that rewards a closer look without adding noise to the rail.

---

## 3. Palette

Contrast is measured with the WCAG 2.x relative-luminance formula. "Ink" is
`#07070b`; "panel" is `#101017` (the lightest body background in the app).

| Token | Hex | Role | On ink | On panel |
|---|---|---|---|---|
| `--paper` / `--text` | `#f2eee3` | body copy, headings on dark | **17.35:1** | **16.34:1** |
| `--muted` | `#a8a399` | secondary copy, rail labels | **8.01:1** | **7.55:1** |
| `--faint` | `#8f8a80` | metadata, timestamps, hints | **5.86:1** | **5.52:1** |
| `--acid` | `#e8ff00` | signal 1 / display / icons / sticker field | **17.94:1** | **16.90:1** |
| `--magenta` | `#ff2d8f` | signal 2 / display / icons / sticker field | **5.77:1** | **5.43:1** |
| `--cyan` | `#22e6ff` | signal 3 / icons / state / sticker field | **13.24:1** | **12.48:1** |
| `--orange` | `#ff6a1a` | warning / player status | **7.02:1** | **6.61:1** |
| `--line` | `rgba(242,238,227,.10)` | hairlines, never text | — | — |
| `--line-bright` | `rgba(242,238,227,.22)` | focus-adjacent rules | — | — |

**Ink on the sticker fields.** The rail art is a black ink plate on one spot
colour. These pairs are for graphics, but they are strong enough that even a
large label would pass:

| Field | Hex | Ink glyph | Notes |
|---|---|---|---|
| acid | `#e8ff00` | **17.94:1** | Latest, DNB, Search |
| magenta | `#ff2d8f` | **5.77:1** | Kool/Live, Podcasts, Favourites |
| cyan | `#22e6ff` | **13.24:1** | By Year, and the radio masts |
| paper | `#f2eee3` | **17.35:1** | Tape Packs, Legacy Sites |

The colour field is never the only signal: every rail item also has its label,
and every glyph has a different silhouette.

**Rules**

- Body text is `--paper`, `--muted` or `--faint`; all three clear 4.5:1 on every
  surface used for copy, including the player and the queue.
- **Acid, magenta and cyan are not body-copy colours.** They are for display
  type at 18 px+/bold, icons, rules, borders, indicators and sticker fields
  with black ink on top. Magenta technically passes on ink (5.43:1) but is
  still kept out of paragraphs because it vibrates at small sizes.
- Ink on acid (the play button, the skip link, hover states) is 17.9:1; ink on
  the magenta, cyan and paper sticker fields is 5.77:1, 13.24:1 and 17.35:1
  respectively.
- Error text `#ffd0e2` on the magenta wash is >10:1; warning text `#ffd9c2` on
  the orange wash is >8:1.
- The hero has a left-to-right scrim (97% ink at the left edge) and a bottom
  mask. The page title is always over the dark end, never over the sun.
- Hover is never the only state: rows also gain a border and a lift; focus is a
  2 px acid outline; the player uses `aria-pressed` / `aria-current`.

The lighter greys that were in the old stylesheet (`#9498a8`, `#6b6f80`) were
retired because the second one sat at roughly 3.9:1 on the panel.

---

## 4. Type hierarchy

No webfonts are loaded. The app uses the system's condensed display faces and
its system UI face; this keeps the page free of font files and third-party
requests, and matches the "photocopied flyer on a cheap screen" intent.

| Role | Family | Size / treatment |
|---|---|---|
| Wordmark | `--font-display` | 26 px (21 px phone), 0.86 line-height, acid + paper, 3 px offset shadows |
| Page title | `--font-display` | `clamp(30px, 3.8vw, 48px)` caps, 4 px acid rule above, magenta offset |
| Player / help display | `--font-display` | 28 px caps, same acid rule |
| UI | `--font-ui` | 12.5–16 px, 600–650 weight; never all-caps except micro-labels |
| Row title | `--font-ui` | 14 px / 650, paper |
| Metadata | `--font-mono` | 9–11 px, 0.02–0.16 em tracking, `--faint` |
| Catalogue / status | `--font-mono` | 9 px, uppercase, wide tracking |
| Code / keys | `--font-mono` | 10 px, panel-2 chip |

`--font-display` resolves to Impact, then Haettenschweiler, then Arial Narrow
Bold. Linux builds fall back to a condensed bold sans; the hierarchy still
holds because the size and the rule do the work, not the exact face.

---

## 5. Texture and print language

- **Halftone field** — 9 px acid dots at ~5% opacity, with a second 13 px
  magenta screen offset by (4, 5) px and a 45° 1 px hatch at 1.3%. This is the
  global `.halftone` layer; it is fixed, pointer-events none, and sits behind
  all content.
- **Sticker tile** — every section icon is a 512 px tile: one flat spot colour,
  an ink halftone screen, a paper sheen, a 2 px ink keyline, four crop marks and
  a catalogue code. The glyph is an **ink plate**: bright fills become solid
  ink, dark fills and keylines become holes. At 22 px only the field and the
  black silhouette survive, which is the intended degradation; at 52 px the
  screen, marks and codes appear.
- **Misregistration** — the wordmark, the app icon and the hero carry the
  second-pass offset (magenta behind black/acid). The section glyphs are flat
  ink plates instead, because a 22 px rail needs a clean silhouette more than a
  print effect.
- **Hazard tape** — a 3 px repeating acid bar sits under the topbar at 24%
  opacity, like the edge of a flyer.
- **Hero grain** — the hero is finished with a light noise blend and a halftone
  screen in the highlights, so it compresses well and does not look like a
  vector gradient.
- **Motion** — a 3 px "transmission" line scans the top of the player, and the
  now-playing row uses a three-bar level meter. Both are decorative; both stop
  under `prefers-reduced-motion: reduce` (the global reduced-motion rule sets
  animation duration to ~0).

---

## 6. Logo and app icon

**Mark.** A 38 px acid sticker (34 px on phones), rotated −3°, with a black
wink/play smiley drawn inline as SVG. A magenta 3 px offset makes it look
printed. The masthead uses a **line version** of the face so it stays open at
38 px; the app icon uses the solid black plate version. They share the same
wink/smile geometry and the same acid stock.

**Wordmark.** `RAVE` in acid with a magenta offset; `PLAYER` in paper with a
cyan offset. It is set in the display face, not an image, so it stays sharp at
any DPR and costs nothing. The strap is mono: `TRANSMISSION ARCHIVE · 88 → NOW`.

**App icon.** 512 × 512 `icon.png`: acid field, a light ink halftone, a huge
black face plate, acid eyes/smile, a magenta second-pass disc, and a right eye
that is a play triangle. No text, no border, no transparency; it is full-bleed
and safe for iOS/Android masking. It is deliberately the loudest acid object in
the product — the face on the home screen. `icon-180.png` is the 180 px
apple-touch rendering (same drawing, resized with Lanczos). The manifest still
points at `icon.png` 512; the template adds the 180 px apple-touch link.

---

## 7. Asset inventory

All paths are under `static/art/`. "Slot" is the rail/menu label that uses the
file in `app.py`; the mapping lives in `SECTIONS`, `RAIL`, `PACK_ART`,
`LIVE_ART` and `FILTER_ART` (1.6.40).

### Section / filter icons — 512 × 512 PNG (a few shared 256 px copies)

| Group | Files |
|---|---|
| Brand / root | `rave.png` (Latest), `radio.png` (Live Radio), `tapepacks.png` (Tape Packs), `search.png`, `favourites.png`, `downloads.png`, `about.png` |
| Rail hubs | `mixes.png` (Mixes & Shows), `curated.png` (Curated Gems), `sets.png` (Club & Rave Sets) |
| Stations | `kool.png`, `rinse.png`, `oldskool.png`, `radio-jungle.png`, `radio-dnb.png`, `radio-hardcore.png`, `radio-house.png`, `radio-uk-garage.png`, `radio-breakbeat.png` |
| Tape genres | `rave-tapes.png`, `jungle.png`, `hardcore.png`, `house.png`, `garage.png`, `pirate-radio.png`, `tapepacks-all.png`, `years.png`, `series.png`, `rave-nights.png` |
| Shows / sources | `podcasts.png`, `dnb.png`, `moxser.png`, `hardscore.png`, `toronto.png`, `getdarker.png`, `rave-archive.png`, `pirate-archive.png`, `legacy.png`, `ravetapepacks.png`, `jungletapes.png` |
| Sets widgets | `mixcloud.png`, `soundcloud.png` |
| Quick Filters | `djez.png`, `garage-nation.png` and twelve series marks (`dreamscape.png`, `helter-skelter.png`, `fantazia.png`, `one-nation.png`, `centreforce.png`, `don-fm.png`, `slammin-vinyl.png`, `one-in-the-jungle.png`, `jungle-fever.png`, `roast.png`, `telepathy.png`, `world-dance.png`) |

Since 1.6.40 every navigation row has its own file; distinct rows no longer
share a tile. Leaf rows (tracks, episodes, years, rave nights, station
recordings) deliberately reuse their parent category sticker. The duplicate
guard is `test/test_art_duplicates.py`; the shared one-tile leaf lists are
listed there explicitly.

### Brand / display

| File | Size | Use |
|---|---|---|
| `icon.png` | 512 × 512, 25 KB | favicon, manifest icon, install/home screen |
| `icon-180.png` | 180 × 180, 9 KB | `apple-touch-icon` |
| `hero.jpg` | 1920 × 1080, 239 KB | desktop stage backdrop (CSS media query) |
| `hero-960.jpg` | 960 × 480, 49 KB | phone stage backdrop; 2:1 crop of the mast and sun |
| `fanart.jpg` | 1920 × 1080, 239 KB | legacy Kodi fanart path in `core/kodi.py`; same artwork as the hero |

Every icon is ≤ 150 KB; the desktop hero is ≤ 600 KB; the phone hero is a
separate, much smaller file. The app adds no webfonts and no icon font.

---

## 8. Integration

### CSS
`static/app.css` keeps the existing variable names (`--bg`, `--panel`, `--text`,
`--acid`, …) so no template or JS had to change, and adds semantic tokens
(`--paper`, `--ink`, `--font-display`, `--font-ui`, `--font-mono`,
`--acid-wash`, …). Layout values (`--rail`, `--player`, `--topbar-h`) are
unchanged; the topbar and rail are still measured by the existing JS.

The hero adds no DOM element and no layout shift: `.stage::before` places it
behind the top of the page with a two-axis scrim and a mask, at 55% opacity
(62% on phones). The desktop rule loads `hero.jpg`; the ≤860 px rule swaps to
`hero-960.jpg`, so phones do not download the 1920 px file. It is
`pointer-events: none` and scrolls with the page (no
`background-attachment: fixed`, which stutters on iOS). The list sits on a 55%
ink field of its own, so the artwork cannot stripe through the row gaps and
the rows keep their own near-opaque treatment.

The phone rail now shows the 22 px icons next to the chip labels (the old CSS
hid them below 860 px). This is the one deliberate layout-affecting change:
the chips are still 44 px touch targets and the rail still scrolls
horizontally, but the sections are now scannable by shape as well as text. The
bright sticker fields are intentionally the busiest thing on the page; the
stage, rows and player stay dark and quiet so the archive remains readable.

#### Embed player dock
Since 1.6.45 the Mixcloud/SoundCloud widget is an inline dock in the main
player's bottom slot, not a modal card. It shares the player's ink deck and
hazard-tape top edge, but there is no scrim and no page scroll lock. Static
hooks:

| Class | Role |
|---|---|
| `.embed-dock` | fixed bottom deck (`--panel` at 97% ink), 1 px `--line-bright` top border, 2 px acid hazard edge; `[hidden]` closes it |
| `.embed-dock-inner` | `max-width: 1180px`, centred, 10 px top pad |
| `.embed-head`, `.embed-heading`, `.embed-kicker`, `.embed-title`, `.embed-actions` | wrapped title row (mono "WIDGET DECK" kicker + display heading) and actions |
| `.embed-link` | mono "Open on …" pill; acid hover |
| `.embed-close` | 44 × 44 Close target; `--panel` field, acid focus ring |
| `.embed-frame-wrap` | capped frame: `height: clamp(156px, 24vh, 210px)` desktop, `clamp(150px, 27vh, 190px)` at ≤620 px; `--radius` clip, panel/hatch fallback |
| `.embed-frame` | absolutely positioned iframe, 100% × 100%, no border, `z-index: 1` |
| `.embed-placeholder` | centred mono loading label with an acid square; `z-index: 2` until `is-loaded` |
| `.embed-note` | mono live status (`aria-live="polite"`), `--faint` |
| `body.has-embed` | reserves `--embed-dock-h` in the stage/rail; does **not** lock scrolling |

The wrapper is a neutral panel/hatch tile, so a blank or slow iframe is never a
white flash or a dead black hole. The labelled loading state is built in: the
markup ships `is-loading` + `.embed-placeholder`; the iframe's `load` event
swaps to `is-loaded`, and a new set resets it. The placeholder is a status, not
a decorative overlay, and covers the frame only while loading.

```html
<div class="embed-frame-wrap is-loading">
  <div class="embed-placeholder" role="status" aria-live="polite">
    <span id="embed-status">Loading Mixcloud…</span>
  </div>
  <iframe class="embed-frame" title="Mixcloud player" allow="autoplay; encrypted-media"
          loading="lazy" referrerpolicy="no-referrer-when-downgrade"></iframe>
</div>
```

The frame is wider on desktop and capped rather than 16:9, so a 1280 px window
gets a 210 px player and a 390 px phone gets a 190 px player without taking over
the viewport. `docs/AUDIO-SINGLE-SOURCE.md` covers the audio policy and the
cross-origin widget bridge.

### HTML
In this artwork pass only the head icon links, `theme-color`, `color-scheme`
and the brand mark changed. All IDs, ARIA hooks, form structure and the player
markup are intact. The app icon is linked as a 512 favicon and a 180
apple-touch icon. The embed dock sits after the player footer; its loading
shelf is part of the markup contract above.

### JS
The artwork pass itself does not edit `app.js`. The existing code still builds
the rail, rows, player, queue, AirPlay and Media Session as before; the embed
wiring (opening the dialog, setting the iframe `src`, toggling the loading
class) is a separate change.

---

## 9. Accessibility notes

- Body copy is 4.5:1 or better on every surface; the lowest pair used for copy
  is `--faint` on `--panel` at 5.52:1.
- The accent colours are not used for body copy; they appear as display type,
  icons, rules, borders, focus rings and sticker fields with black ink on top.
  Ink clears 4.5:1 on all four fields (lowest: magenta, 5.77:1).
- Colour is never the only cue: every rail item has its text label, and every
  glyph has a different silhouette. The four field colours repeat across the
  set on purpose.
- The hero's left scrim and bottom mask keep the page title on near-solid ink;
  the artwork never sits under paragraph text unshielded.
- Crop marks and catalogue codes on icons are decorative; the rail label is
  always present as text.
- The embed placeholder is decorative (`aria-hidden`, `pointer-events: none`,
  no animation); the dialog title, close button and external Mixcloud link carry
  its meaning. It covers the frame only while `is-loading` is set.
- The now-playing meter is decorative; its state is also announced by the
  `aria-current` row, the play/pause button label and the player.
- `prefers-reduced-motion` disables the scan line, the level meter, the
  skeleton shimmer and all transitions.
- Focus is a 2 px acid ring with 2 px offset; the acid play button uses a paper
  ring so it is visible on its own background.

---

## 10. Regenerating

Pillow is a **build-time** dependency only. It is intentionally not in
`requirements.txt`; the Docker image stays Flask + gunicorn.

```sh
# the bundled runtime used for the committed set
/home/user/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/bin/python3 scripts/make-art.py

# duplicate-fix stickers (run from ../raveplayer; writes Kodi 256 + web 512)
python3 ../raveplayer/scripts/make-duplicate-art.py

# icons only, or hero only
python3 scripts/make-art.py --only icons
python3 scripts/make-art.py --only hero

# write a review contact sheet to the system temp dir
python3 scripts/make-art.py --only icons --contact
```

The script writes every file in the inventory above, including `fanart.jpg`,
and prints the byte size of each. It is deterministic: the random seeds for the
stars, skyline and crowd are fixed, so a rebuild should produce identical art.
Only Pillow and the standard library are used; there is no network access.

**Fonts.** The pictogram plates are primitives and do not need a font; only the
cassette label (`RAVE`, `C90`), the calendar's `19`/`93`, the garage's `2` and
`2 STEP`, the DJ EZ `SELECTOR` caption and the catalogue codes do. The display
face is looked up at
`/System/Library/Fonts/Supplemental/Impact.ttf` (macOS), then common Linux
Impact/Liberation/DejaVu condensed paths, then Pillow's default. The mono face
uses SF Mono, Menlo, DejaVu Mono or Liberation Mono in that order. If none of
the condensed faces exist, the generated type falls back to a wider bold face;
the icon pictograms still work, only the date and catalogue microtype change
proportion.

**Reviewing.** `contact_sheet()` (used by `--contact`) renders every icon at
22, 44, 52, 128 and 256 px on ink, which is the fastest way to catch a glyph
that stops reading at rail size. The committed set was checked at 22 px and in
the full 512 px previews; the contact sheet is a review artefact, not a shipped
asset. `glyph_to_ink()` is the two-colour pass: bright fills become the black
plate, dark fills become holes, and `save_png()` quantises the finished tiles to
a 256-colour palette so the textures stay small.

---

## 11. Verification

- `./.venv/bin/python -m pytest test -q` → 203 passed (offline suite; the
  headless-browser smoke suite adds 7 more via `pytest test_e2e`).
- `node --check static/app.js` → clean.
- `gunicorn -b 127.0.0.1:8101 app:app`, then `curl` `/`, `/static/app.css` and
  every referenced `/static/art/*` → 200; server stopped afterwards.
- Icon dimensions 512 × 512 (eleven section/filter tiles plus the app icon),
  apple-touch 180 × 180, hero 1920 × 1080 and 960 × 480; all PNGs ≤ 150 KB
  (largest: `radio.png` at 90 KB; `garage.png` 78 KB, `djez.png` 82 KB) and the
  hero ≤ 600 KB (239 KB).
- No downloaded, scanned or stock imagery; no trademarked marks; all shapes are
  Pillow primitives in `scripts/make-art.py`.

One pre-existing value is outside this pass: `app.py` still sets the web
manifest's `theme_color` to acid (`#e8ff00`). The page's `<meta name="theme-color">`
is ink (`#0a0a0f`). If the installed Android status bar should also be ink, that
one manifest value is the only remaining change; `app.py` was left untouched as
required.
