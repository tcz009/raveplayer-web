# Legacy stream URLs (RaveTapePacks / JungleTapes / Hardscore)

RaveTapePacks MP3s are hotlink-protected: a bare GET returns `403
text/html`; the same URL returns `200 audio/mpeg` (or `206` with a Range)
only when it carries a browser `Referer` and `User-Agent`.  Kodi sends the
`|Referer=...&User-Agent=...` suffix as real headers; the web `/api/stream`
proxy splits the same suffix.

## What was broken

- `packs()` hard-coded `https://www.ravetapepacks.com/music/...` from a regex
  capture, discarding the scraped scheme/host and unescaping HTML entities
  twice (once in the caller, once in `_url_path`).  A filename that literally
  contains `&amp;` became `&` and 404'd; `http://` and relative links were
  rebuilt by string concatenation instead of resolved against the page.
- JungleTapes' `href` regex required `.mp3` immediately before the quote, so
  the site's current `.mp3?uuid=...` links were dropped in both builds.
- The web proxy had no recovery when a hotlink guard tightened its Referer
  check; Kodi's `open_media` crashed on a header suffix because `import
  urllib.request` shadowed the module-level `urllib`, and `probe_media`
  ignored the suffix entirely.

## Contract now

- One resolver (`legacy._music_url`) decodes entities **once**, resolves
  relative/protocol-relative/absolute links against the page, forces TLS for
  RaveTapePacks, validates host and two-segment `/music/<pack>/<file>.mp3`
  paths, then percent-encodes the path exactly once.
- `packs()` reads `<audio src>`, `<a href>` and the JSON playlist (escaped
  slashes), de-duplicates by URL.
- `playable()` derives the Referer origin from the URL (including a port for
  fixture servers) and never double-appends a saved suffix.
- `/api/stream` retries a `401/403` once with the file's pack directory as
  Referer (RaveTapePacks only), still through the SSRF/DNS-pinning checks.
- `probe_media()` and `open_media()` both honour the suffix headers.

## Verification

```sh
# web
./.venv/bin/python -m pytest test -q
./.venv/bin/python scripts/audit_legacy_links.py --limit 10

# Kodi
python3 -m unittest discover -s test -v
python3 scripts/audit_legacy_links.py --limit 10
```

`audit_legacy_links.py` samples links from the parsers, adds the
Referer/UA suffix, requests `Range: bytes=0-1`, and reports the pass rate
(`206 audio/mpeg` expected).  `--bare` shows the hotlink-403 delta.

Fixtures: `test/fixtures/ravetapepacks-broken.html` covers spaces, `&amp;`,
`&amp;amp;`, relative, protocol-relative, `http://`, JSON-escaped and
traversal/foreign-host shapes.  `test_legacy_urls.py` / `test_legacy_streams.py`
serve those URLs from a local HTTP server that enforces the Referer/UA
contract and returns `206` for a Range request.

Note: the site's firewall rate-limits/blocks an IP after repeated bare 403s.
Run the live audit from a normal client network, not a burst-testing host.
