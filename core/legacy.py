# -*- coding: utf-8 -*-
"""Legacy sources that are genuinely still alive.

The 2014 add-on scraped ~30 fan sites; almost all are gone. These survived and
still serve full tape packs, Toronto jungle radio and 1990s rave mixtapes.

RaveTapePacks.com's MP3s are hotlink-protected — a bare GET returns 403 — so
playback URLs carry a Referer suffix that Kodi turns into a real header.
"""

import hashlib
import html
import re
import urllib.parse

from . import cache
from . import fetch as http

BASE = "https://www.ravetapepacks.com/"
BROWSER_UA = "Mozilla/5.0"
REFERER_SUFFIX = "|Referer=https://www.ravetapepacks.com/&User-Agent=" + BROWSER_UA
# The site 403s Python's default urllib UA; a browser UA also keeps the page
# fetch (and the category pages) out of its bot filter.
PAGE_HEADERS = {"User-Agent": BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}

PACKS_TTL = 6 * 60 * 60
SITEMAP_TTL = 24 * 60 * 60
SHOW_TTL = 6 * 60 * 60
TORONTO_TTL = 6 * 60 * 60

# Parsed page objects live beside the raw body in the same cache.  The body is
# still read (and fingerprinted) so a fixture/mock that changes the page cannot
# be served a stale parse; a matching fingerprint skips the regex pass.  Keys
# are versioned so a parser change does not reuse an old shape.
RTP_PACKS_KEY = "legacy:rtp:packs:v1"
RTP_CATEGORIES_KEY = "legacy:rtp:categories:v1"
RTP_CATEGORY_KEY = "legacy:rtp:category:v1:%s"

# The site mixes three shapes: <audio src> with absolute or relative URLs,
# ordinary <a href> links on category pages, and a JSON playlist whose slashes
# are escaped (``https:\/\/...``).  Match all three; the resolver below is the
# only thing that turns them into a URL, so an entity is decoded exactly once.
MP3_ATTR_RE = re.compile(
    r'<(?:audio|source)\b[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
MP3_HREF_RE = re.compile(
    r'<a\b[^>]*\bhref\s*=\s*["\']([^"\']+\.mp3(?:\?[^"\']*)?)["\']', re.IGNORECASE)
MP3_JS_RE = re.compile(
    r'https?:\\?/\\?/[^"\'<>\s]+?\.mp3(?:\?[^"\'<> ]*)?', re.IGNORECASE)

# Kept for callers/tests that want the old capture groups.
MUSIC_RE = re.compile(
    r"(?:https?://www\.ravetapepacks\.com)?/music/([^/\"'<>]+)/([^\"'<>]+?\.mp3)",
    re.IGNORECASE,
)


def _words(text):
    """Space out camelCase/letter-digit joins in already-decoded text."""
    text = str(text or "").replace("_", " ")
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[0-9])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Za-z])(?=[0-9])", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _space_words(value):
    """Unquote, unescape and split camelCase/letter-digit joins."""
    return _words(html.unescape(urllib.parse.unquote(str(value or ""))))


def _url_path(value):
    """Return a correctly quoted URL path, without double-encoding.

    Scraped paths are third-party input: reject empty/"."/".." segments so a
    malformed link cannot walk up the site path (mirrors the add-on).
    """
    text = html.unescape(urllib.parse.unquote(str(value or "")))
    parts = text.split("/")
    if text.startswith("/"):
        parts = parts[1:]
    if not parts or any(part in ("", ".", "..") for part in parts):
        return ""
    return urllib.parse.quote(text, safe="/")


def _pretty(text):
    """Raw-input helper (kept for callers/tests); parsing uses :func:`_display`."""
    return _space_words(text)


def _pack_name(raw):
    """Folder names are camel-cased and run words together; space them out."""
    return _space_words(raw) or str(raw)


def _display(value):
    """Name for a value the URL resolver already decoded exactly once.

    Re-running ``html.unescape``/``unquote`` here would turn a literal
    ``&amp;`` filename back into ``&`` (and a literal ``%20`` into a space),
    which is the double-decode bug this pass removes.
    """
    return _words(value)


def _decode_url(value):
    """Decode HTML entities (and JSON ``\\/`` escapes) **once**.

    Scraped attributes are HTML source.  ``&amp;`` means ``&`` in the real
    filename, while a filename that really contains ``&amp;`` is written
    ``&amp;amp;`` in the page.  Decoding here and never again is what keeps
    those two cases apart; the old ``_packs_from_html()`` + ``_url_path()``
    pair unescaped twice and turned a literal ``&amp;`` filename into ``&``.
    """
    text = html.unescape(str(value or "")).strip()
    return text.replace("\\/", "/")


def _allowed_host(host, base):
    host = (host or "").lower().rstrip(".")
    if host == "ravetapepacks.com" or host.endswith(".ravetapepacks.com"):
        return True
    try:
        base_host = (urllib.parse.urlsplit(base).hostname or "").lower().rstrip(".")
    except ValueError:
        base_host = ""
    return bool(host and base_host and host == base_host)


def _music_url(raw, base=None):
    """Resolve one scraped MP3 reference to ``(pack, filename, url)``.

    Handles absolute, protocol-relative and page-relative URLs, an ``http://``
    link on an ``https`` page, spaces/unicode and HTML entities.  The path is
    unquoted once, checked for traversal, then quoted once; the host must be
    RaveTapePacks (or the page's own host when tests point ``BASE`` at a local
    server).  Returns ``None`` for anything that is not a two-segment
    ``/music/<pack>/<file>.mp3`` path.
    """
    base = BASE if base is None else base
    text = _decode_url(raw)
    if not text:
        return None
    if text.startswith("//"):
        text = "https:" + text
    try:
        joined = urllib.parse.urljoin(base, text)
        split = urllib.parse.urlsplit(joined)
    except ValueError:
        return None
    if split.scheme.lower() not in ("http", "https") or not split.hostname:
        return None
    host = split.hostname.lower().rstrip(".")
    if split.username is not None or split.password is not None:
        return None
    if not _allowed_host(host, base):
        return None
    # The page is served over TLS and many of its own links still say
    # http://; use TLS for this host so the browser never sees mixed content
    # and the proxy never has to follow a per-stream redirect.
    scheme = "https" if host.endswith("ravetapepacks.com") else split.scheme.lower()
    netloc = split.netloc.lower()
    if host == "ravetapepacks.com":
        netloc = "www.ravetapepacks.com"
    path = urllib.parse.unquote(split.path)
    segments = path.split("/")
    if len(segments) != 4 or segments[1].lower() != "music":
        return None
    pack, filename = segments[2], segments[3]
    if any(part in ("", ".", "..") for part in segments[1:]):
        return None
    if not filename.lower().endswith(".mp3"):
        return None
    quoted = urllib.parse.quote(path, safe="/")
    return pack, filename, urllib.parse.urlunsplit(
        (scheme, netloc, quoted, split.query, ""))


def _music_refs(page):
    """Yield ``(pack, filename, url)`` for every MP3 reference, de-duplicated."""
    seen = set()
    for regex in (MP3_ATTR_RE, MP3_HREF_RE, MP3_JS_RE):
        for raw in regex.findall(page or ""):
            item = _music_url(raw)
            if not item:
                continue
            if item[2] in seen:
                continue
            seen.add(item[2])
            yield item


def _packs_from_html(page):
    grouped = {}
    for pack, filename, url in _music_refs(page):
        entry = grouped.setdefault(
            pack, {"key": pack, "name": _display(pack) or str(pack), "tracks": []})
        # ``filename`` comes from the resolver's one unquote/entity decode; do
        # not decode it again (a literal ``&amp;``/``%20`` must survive).
        entry["tracks"].append(
            {"name": _display(filename.rsplit(".", 1)[0]), "url": url})
    result = []
    for entry in grouped.values():
        entry["tracks"].sort(key=lambda track: track["name"])
        result.append(entry)
    result.sort(key=lambda entry: entry["name"].lower())
    return result


def _fingerprint(page):
    """Stable (cross-process) fingerprint of the exact page text."""
    return hashlib.blake2b(page.encode("utf-8", "replace"), digest_size=8).hexdigest()


def _cached_parse(key, ttl, page, parser):
    """Parse ``page`` once per page version, cache the parsed object.

    The body is still read (from the existing HTTP cache) so the fingerprint
    can prove the parse matches this page.  That keeps tests/fixtures that swap
    the body from getting a stale parse, while a normal warm call pays only a
    cache read instead of the regex pass.
    """
    if page is None:
        return None
    fingerprint = _fingerprint(page)
    record = cache.get(key, ttl)
    if isinstance(record, dict) and record.get("fingerprint") == fingerprint:
        value = record.get("value")
        if isinstance(value, (list, dict)):
            return value
    value = parser(page)
    cache.put(key, {"fingerprint": fingerprint, "value": value}, sync=False)
    return value


EMPTY_CATEGORIES = {"events": [], "djs": [], "mcs": [], "years": []}


def _homepage():
    """Fetch the homepage once, with the browser headers the site requires.

    Both ``packs()`` and ``categories()`` read this one cache entry.  The old
    ``packs()`` sent the default RavePlayer UA, so it cached under a different
    header key and a pack-detail click fetched/parsed the homepage a second
    time (and could be 403'd by the bot filter).
    """
    return http.fetch(BASE, timeout=25, ttl=PACKS_TTL, headers=PAGE_HEADERS)


def packs():
    """Return [{'key': folder, 'name': label, 'tracks': [...]}, ...] sorted by name."""
    page = _homepage()
    return _cached_parse(RTP_PACKS_KEY, PACKS_TTL, page, _packs_from_html) or []


_CATEGORY_HEADINGS = {
    "PACKS BY EVENT": "events",
    "PACKS BY DJ": "djs",
    "PACKS BY MC": "mcs",
    "PACKS BY YEAR": "years",
}
_MENU_ITEM_RE = re.compile(
    r'<li[^>]*>\s*<a[^>]*>\s*(PACKS BY (?:EVENT|DJ|MC|YEAR))\s*</a>\s*'
    r'<ul[^>]*>(.*?)</ul>', re.I | re.S)
_CAT_RE = re.compile(r'\?cat=(\d+)"[^>]*>(.*?)</a>', re.I | re.S)


def parse_categories(page):
    """Parse the site nav's PACKS BY EVENT/DJ/MC/YEAR menus (no extra fetch)."""
    groups = {"events": [], "djs": [], "mcs": [], "years": []}
    for match in _MENU_ITEM_RE.finditer(page or ""):
        kind = _CATEGORY_HEADINGS.get(match.group(1).upper())
        if not kind:
            continue
        seen = set()
        for cat_id, label in _CAT_RE.findall(match.group(2)):
            name = html.unescape(re.sub(r"<[^>]+>", "", label)).strip()
            if not name or cat_id in seen:
                continue
            seen.add(cat_id)
            groups[kind].append({"id": cat_id, "name": name})
    return groups


def _build_categories(page):
    return parse_categories(page)


def categories():
    page = _homepage()
    parsed = _cached_parse(RTP_CATEGORIES_KEY, PACKS_TTL, page, _build_categories)
    return parsed if isinstance(parsed, dict) else dict(EMPTY_CATEGORIES)


def category(kind, cat_id):
    entry = next((item for item in categories().get(kind, [])
                  if item["id"] == str(cat_id)), None)
    return entry


CATEGORY_TTL = 6 * 60 * 60


def category_packs(kind, cat_id):
    """Packs carrying one RaveTapePacks category id.

    The id is validated as digits and then fetched directly.  The old code
    first looked the id up in the homepage nav, which on a cold deep link made
    a category view fetch and parse the homepage before the category page — the
    duplicate page fetch this pass removes.
    """
    if kind not in ("events", "djs", "mcs", "years"):
        return []
    cat_id = str(cat_id or "")
    if not cat_id.isdigit():
        return []
    page = http.fetch(BASE + "?cat=" + cat_id, timeout=25, ttl=CATEGORY_TTL,
                      headers=PAGE_HEADERS)
    return _cached_parse(RTP_CATEGORY_KEY % cat_id, CATEGORY_TTL,
                         page, _packs_from_html) or []


def pack(key, kind=None, cat_id=None):
    """Resolve one pack, preferring the category that linked it.

    A category link carries ``kind``/``cat``: checking that (cached) category
    page first means a cold pack detail does not parse the whole homepage
    index.  Links without a category (the "All Packs" list) use the index.
    """
    key = str(key or "")
    if not key:
        return None
    if kind in ("events", "djs", "mcs", "years") and str(cat_id or "").isdigit():
        for entry in category_packs(kind, cat_id):
            if isinstance(entry, dict) and entry.get("key") == key:
                return entry
    for entry in packs():
        if isinstance(entry, dict) and entry.get("key") == key:
            return entry
    return None


def playable(url):
    """Kodi header syntax: append the Referer/UA the hotlink check requires.

    The origin is derived from the URL so a mirror host, a saved favourite or
    the local fixture server gets a Referer that matches its own request.  A
    URL that already carries a header suffix is returned untouched.
    """
    text = str(url)
    if "|" in text:
        # Already has a header suffix (e.g. a saved favourite); never double it.
        return text
    origin = "https://www.ravetapepacks.com/"
    try:
        split = urllib.parse.urlsplit(text)
        host = (split.hostname or "").lower()
    except ValueError:
        host = ""
        split = None
    if host and not (host == "ravetapepacks.com" or host.endswith(".ravetapepacks.com")):
        netloc = split.netloc or host
        origin = "%s://%s/" % ((split.scheme or "https") if split else "https", netloc)
    return text + "|Referer=" + origin + "&User-Agent=" + BROWSER_UA


# --------------------------------------------------------------------------- jungletapes.com

JT_BASE = "https://jungletapes.com"
JT_SHOW_RE = re.compile(r'href="/show/([^"]+)"[^>]*>.*?</a>', re.IGNORECASE | re.DOTALL)
JT_MP3_RE = re.compile(
    r'href="([^"]+\.mp3(?:\?[^"]*)?)"', re.IGNORECASE)


def jungle_shows():
    """Toronto jungle/DnB radio shows digitised from cassette — one page each.

    The site's front page only lists the newest two, so the sitemap is the
    catalogue: 300+ /show/ pages.
    """
    slugs = []
    # /tapes is the current catalogue (400+ entries); the sitemap catches
    # anything older that has dropped off it. Union both, then de-dupe below.
    index = http.fetch(JT_BASE + "/tapes", timeout=30, ttl=SITEMAP_TTL)
    if index:
        slugs.extend(re.findall(r'href="/show/([^"]+)"', index))
    xml = http.fetch(JT_BASE + "/sitemap.xml", timeout=30, ttl=SITEMAP_TTL)
    if xml:
        slugs.extend(re.findall(r"<loc>[^<]*?/show/([^<]+)</loc>", xml))
    if not slugs:
        page = http.fetch(JT_BASE + "/", timeout=25, ttl=SITEMAP_TTL) or ""
        slugs = list(JT_SHOW_RE.findall(page))
    shows, seen = [], set()
    for slug in slugs:
        slug = slug.strip().strip("/")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        shows.append({"key": slug, "name": _jt_label(slug)})
    shows.sort(key=lambda item: item["name"], reverse=True)
    return shows


def _jt_label(slug):
    text = html.unescape(slug).replace("tapes/", "").replace("_", " ").replace("-", " ")
    text = re.sub(r"\b000000\b", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1].upper() + text[1:] if text else slug


def jungle_tracks(slug):
    quoted = _url_path(slug)
    if not quoted:
        return []
    page = http.fetch(JT_BASE + "/show/" + quoted, timeout=25, ttl=SHOW_TTL)
    if not page:
        return []
    tracks, seen = [], set()
    for url in JT_MP3_RE.findall(page):
        # Decode exactly once (mirrors the add-on): the href may contain
        # ``&amp;`` for a real ampersand or a JSON ``\/`` escape.
        url = _decode_url(url)
        if url.startswith("//"):
            url = "https:" + url
        url = urllib.parse.urljoin(JT_BASE + "/", url)
        split = urllib.parse.urlsplit(url)
        # The site emits absolute http:// links for its own host and
        # www.jungletapes.com 302s to the apex for every file.  Canonicalise
        # both here so the proxy does not follow a redirect per stream.
        host = (split.hostname or "").lower()
        if host.endswith("jungletapes.com"):
            canonical = "jungletapes.com" if host == "www.jungletapes.com" else host
            if split.scheme.lower() != "https" or host == "www.jungletapes.com":
                url = urllib.parse.urlunsplit(
                    ("https", canonical, split.path, split.query, ""))
                split = urllib.parse.urlsplit(url)
        # Never pass a scraped file:/javascript:/data: URL to the browser.
        if split.scheme.lower() not in ("http", "https") or not split.netloc:
            continue
        if url in seen:
            continue
        seen.add(url)
        filename = urllib.parse.unquote(url.rsplit("/", 1)[-1]).rsplit(".", 1)[0]
        tracks.append({"name": _display(filename), "url": url})
    return tracks
