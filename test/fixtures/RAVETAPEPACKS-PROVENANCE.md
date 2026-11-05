# RaveTapePacks navigation fixtures

Third-party markup kept only to exercise the parser offline.  Captured through
the Internet Archive (`id_` raw snapshots); no test contacts the live site.

| File | Source | Snapshot | Trimmed to |
|---|---|---|---|
| `ravetapepacks-nav.html` | `https://www.ravetapepacks.com/` | 2024-08-09 | the site `<nav>` with all 362 category anchors |
| `ravetapepacks-category-paged.html` | `https://www.ravetapepacks.com/?cat=5` | 2026-04-12 | 8 post headers/footers and the pager |
| `ravetapepacks-category-paged-2.html` | `https://www.ravetapepacks.com/?cat=5&paged=2` | 2026-03-16 | 2 post headers/footers and the pager |
| `ravetapepacks-post.html` | `https://www.ravetapepacks.com/?p=899` | 2025-08-15 | the `post-899` article (15 `<audio>` players) and post nav |

`ravetapepacks-nav.html` is the same snapshot as `bench/rtp-home.html`; the
bench file is the full 200 KB page used for timing.  The category fixture keeps
the duplicate title/date `rel="bookmark"` links and the theme's
`div.nav-previous` "Older posts" link so the parser is tested against the real
shape, including the backward "Newer posts" link on page 2.
