# Toronto Rave Mixtape Archive source — 1.6.38

The Toronto Rave Mixtape Archive (TRMA) is promoted from a legacy credit to a
first-class web source. It publishes four genre pages of direct MP3s, so every
row plays through the existing same-origin `/api/stream` proxy and can be saved
with the normal direct-file Download path — there is no platform page or
hand-off.

**Credit:** [Toronto Rave Mixtape Archive](https://torontoravemixtapearchive.com/)
— an independent, volunteer-run archive of 1990s Toronto rave recordings. The
site remains the source of the audio; Rave Player only indexes the public
`/files/...mp3` links.

## Pages

| Genre id | Site page | Label |
|---|---|---|
| `jungle` | `/jungle.html` | Jungle & Hardcore |
| `house` | `/house.html` | House & Techno |
| `studio` | `/studio.html` | Studio |
| `xstatic` | `/xstatic.html` | X-Static |

These are all the genre links in the site nav (Updates/Contact are not genre
pages). Pages are cached for **6 hours** and requests are spaced to **≤1/s**;
failures are soft (an empty section, never a 500). Parsing is offline-testable
from `test/fixtures/toronto/*.html`.

## API / UI

- `GET /api/toronto` — hub folders for the four genres.
- `GET /api/toronto?genre=jungle|house|studio|xstatic` — direct rows.
- `GET /api/toronto?q=syrous` — title/venue/date search across all genres.
- Each row carries `label`, `sub` (Toronto · date · venue), `date`, `venue`,
  `duration`, direct `play` URL, local art fallback and
  `download: {"source":"toronto"}`. No platform URL is emitted.
- Added to **Mixes & Shows** (`/api/rail/shows`), the flat section list, client
  route map and `/api/menu`.
- Download eligibility accepts only
  `torontoravemixtapearchive.com` `/files/mixtape/*.mp3` and
  `/files/xstatic/*.mp3`; host/path re-checked server-side like every other
  direct source.

The old `/api/legacy/toronto` route remains for old bookmarks but is no longer
listed under Legacy Sites; the new source is the canonical one.
