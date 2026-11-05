# -*- coding: utf-8 -*-
"""Legacy sources that are genuinely still alive.

The 2014 add-on scraped ~30 fan sites; almost all are gone. These survived and
still serve full tape packs, Toronto jungle radio and 1990s rave mixtapes.

RaveTapePacks.com's MP3s are hotlink-protected — a bare GET returns 403 — so
playback URLs carry a Referer suffix that Kodi turns into a real header.
"""

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

# Audit P0-3: the site intermittently drops the connection.  A 6 s homepage
# deadline keeps the hub inside the 8 s error budget, and a 60 s negative
# cache stops every Retry/child click from paying the timeout again.
RTP_HOMEPAGE_TIMEOUT = 6
RTP_PAGE_TIMEOUT = 6
RTP_FAILURE_KEY = "legacy:rtp:homepage-failure:v1"
RTP_FAILURE_TTL = 60

# Parsed page objects live beside the raw body in the same cache.  The body is
# still read (and fingerprinted) so a fixture/mock that changes the page cannot
# be served a stale parse; a matching fingerprint skips the regex pass.  Keys
# are versioned so a parser change does not reuse an old shape.
RTP_PACKS_KEY = "legacy:rtp:packs:v1"
# 1.6.51: the homepage nav is parsed by document order, not by its <ul>
# nesting, and category/post pages are new cache entries.  Versioned keys keep
# a pre-1.6.51 parse from being reused.
RTP_CATEGORIES_KEY = "legacy:rtp:categories:v2"
RTP_CATEGORY_KEY = "legacy:rtp:category:v2:%s"
# 1.6.52: category views fetch one WordPress page at a time so a large
# category is navigable instead of being silently truncated by the route
# budget, and "All Packs" walks ?cat=0 (the blog archive) instead of the
# homepage's rotating "RANDOM SELECTION" block.
RTP_CATEGORY_PAGE_KEY = "legacy:rtp:category-page:v1:%s:%d"
RTP_ALL_PACKS_KEY = "legacy:rtp:all-packs:v2:%d"
RTP_POST_KEY = "legacy:rtp:post:v1:%s"
CATEGORY_TTL = 6 * 60 * 60
POST_TTL = 6 * 60 * 60
# WordPress archives page at 10 posts by default.  A category with hundreds of
# packs stops here rather than turning one request into a crawl.
MAX_CATEGORY_PAGES = 20
# The blog archive (?cat=0) currently holds ~185 posts at nine per page.
MAX_ALL_PACKS_PAGES = 60
CATEGORY_KINDS = ("events", "djs", "mcs", "years")

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
    return cache.fingerprint(page)


def _cached_parse(key, ttl, page, parser):
    """Parse ``page`` once per page version, cache the parsed object.

    The body is still read (from the existing HTTP cache) so the fingerprint
    can prove the parse matches this page.  That keeps tests/fixtures that swap
    the body from getting a stale parse, while a normal warm call pays only a
    cache read instead of the regex pass.
    """
    return cache.parsed(key, ttl, page, parser)


EMPTY_CATEGORIES = {"events": [], "djs": [], "mcs": [], "years": []}


def homepage_failure_cached():
    """True while the last homepage fetch failed, inside the 60 s window."""
    try:
        return cache.get(RTP_FAILURE_KEY, RTP_FAILURE_TTL) is not cache.MISSING
    except Exception:  # noqa: BLE001 - cache trouble must not break a route
        return False


def _homepage():
    """Fetch the homepage once, with the browser headers the site requires.

    Both ``packs()`` and ``categories()`` read this one cache entry.  The old
    ``packs()`` sent the default RavePlayer UA, so it cached under a different
    header key and a pack-detail click fetched/parsed the homepage a second
    time (and could be 403'd by the bot filter).

    A failed fetch (timeout/refusal) is remembered for 60 s: repeating the hub
    or deep-linking a child must not pay another 25 s timeout (audit P0-3).
    A cached good body always wins over the failure mark.
    """
    cached = http.cached_body(BASE, PACKS_TTL, PAGE_HEADERS)
    if cached:
        return cached
    if homepage_failure_cached():
        return None
    page = http.fetch(BASE, timeout=RTP_HOMEPAGE_TIMEOUT, ttl=PACKS_TTL,
                      headers=PAGE_HEADERS)
    if page is None:
        cache.put(RTP_FAILURE_KEY, {"failed": True}, sync=False)
    else:
        cache.forget(RTP_FAILURE_KEY)
    return page


def packs():
    """Return [{'key': folder, 'name': label, 'tracks': [...]}, ...] sorted by name."""
    page = _homepage()
    return _cached_parse(RTP_PACKS_KEY, PACKS_TTL, page, _packs_from_html) or []


# The homepage exposes its taxonomy twice: the nav's PACKS BY EVENT/DJ/MC/YEAR
# sub-menus, and a flat, alphabetical category widget in the sidebar.  The site
# has changed the wrapper markup between years, so the stable signal is the
# document order of the category anchors: 50 events, then 209 DJs from "Adam F",
# then 90 MCs from "5ive-O", then 13 years from "1992" to "2004".  Anchors after
# the year block are genre/other categories and are intentionally ignored.

_CAT_HREF_RE = re.compile(r"[?&](?:amp;)?cat=(\d+)", re.IGNORECASE)
_POST_HREF_RE = re.compile(r"[?&](?:amp;)?p=(\d+)", re.IGNORECASE)
_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<(?:a|link)\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(
    r"""([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_ENTRY_TITLE_RE = re.compile(
    r"""<h1[^>]*\bclass\s*=\s*["'][^"']*\bentry-title\b[^"']*["'][^>]*>(.*?)</h1>""",
    re.IGNORECASE | re.DOTALL)
_PACKS_HEADING_RE = re.compile(
    r"<li[^>]*>\s*<a[^>]*>\s*(PACKS BY (?:EVENT|DJ|MC|YEAR))\s*</a>\s*"
    r"<ul[^>]*>(.*?)</ul>", re.IGNORECASE | re.DOTALL)
_HEADING_KINDS = {
    "PACKS BY EVENT": "events",
    "PACKS BY DJ": "djs",
    "PACKS BY MC": "mcs",
    "PACKS BY YEAR": "years",
}


def _attr_value(attrs, name):
    """Return one HTML attribute value (double or single quoted), or ``""``."""
    wanted = str(name or "").lower()
    for match in _ATTR_RE.finditer(attrs or ""):
        if match.group(1).lower() == wanted:
            return match.group(2) if match.group(2) is not None else (match.group(3) or "")
    return ""


def _tag_text(raw):
    """Visible text of one scraped fragment, decoded exactly once."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _marker(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip().casefold()


def _category_rows(page):
    """Yield every ``(cat_id, name)`` anchor in document order.

    Duplicate ids are kept here: the site reuses one category id for an event
    and an MC ("Vision" = 33), so a global id de-dupe would silently drop the
    MC row.  Each *kind* de-dupes its own slice instead (see parse_categories).
    """
    rows = []
    for match in _ANCHOR_RE.finditer(page or ""):
        href = _decode_url(_attr_value(match.group(1), "href"))
        found = _CAT_HREF_RE.search(href)
        if not found:
            continue
        name = _tag_text(match.group(2))
        if not name:
            continue
        rows.append((found.group(1), name))
    return rows


def _categories_from_headings(page):
    """Fallback for a page whose category list no longer starts at "Adam F"."""
    groups = {"events": [], "djs": [], "mcs": [], "years": []}
    for match in _PACKS_HEADING_RE.finditer(page or ""):
        heading = re.sub(r"\s+", " ", match.group(1)).strip().upper()
        kind = _HEADING_KINDS.get(heading)
        if not kind:
            continue
        seen = set(entry["id"] for entry in groups[kind])
        for cat_id, name in _category_rows(match.group(2)):
            if cat_id in seen:
                continue
            seen.add(cat_id)
            groups[kind].append({"id": cat_id, "name": name})
    return groups


def parse_categories(page):
    """Classify the homepage category anchors into event/dj/mc/year groups.

    Uses the verified document order and three stable boundary names.  A page
    that no longer contains those markers falls back to the PACKS BY sub-menu
    nesting, so a future theme tweak degrades instead of emptying the indexes.
    """
    rows = _category_rows(page)
    if rows:
        names = [_marker(name) for _cat_id, name in rows]
        try:
            dj_start = names.index("adam f")
            mc_start = names.index("5ive-o", dj_start + 1)
            year_start = next(
                index for index, name in enumerate(names)
                if index > mc_start and re.fullmatch(r"(?:19|20)\d{2}", name))
        except (ValueError, StopIteration):
            dj_start = mc_start = year_start = -1
        if 0 <= dj_start < mc_start < year_start:
            groups = {"events": [], "djs": [], "mcs": [], "years": []}
            for kind, chunk in (("events", rows[:dj_start]),
                                ("djs", rows[dj_start:mc_start]),
                                ("mcs", rows[mc_start:year_start])):
                seen = set()
                for cat_id, name in chunk:
                    if cat_id in seen:
                        continue
                    seen.add(cat_id)
                    groups[kind].append({"id": cat_id, "name": name})
            previous = 0
            for cat_id, name in rows[year_start:]:
                if not re.fullmatch(r"(?:19|20)\d{2}", name):
                    break
                year = int(name)
                # Stop at 2004 and at the sidebar's repeat of 1992; only the
                # ascending nav block is the year index.
                if not 1992 <= year <= 2004 or year <= previous:
                    break
                previous = year
                groups["years"].append({"id": cat_id, "name": name})
            return groups
    return _categories_from_headings(page)


def _build_categories(page):
    return parse_categories(page)


def categories_state():
    """Return the parsed category nav, or ``None`` when the homepage failed.

    ``None`` is different from an empty dict: the hub must not render
    "By Event (0)" as though the source were legitimately empty (audit P0-3).
    """
    page = _homepage()
    if page is None:
        return None
    parsed = _cached_parse(RTP_CATEGORIES_KEY, PACKS_TTL, page, _build_categories)
    return parsed if isinstance(parsed, dict) else dict(EMPTY_CATEGORIES)


def categories():
    parsed = categories_state()
    return parsed if parsed is not None else dict(EMPTY_CATEGORIES)


def cached_categories():
    """Category nav from what is already cached — never a new homepage fetch.

    A category view only wants the title (the posts come from their own
    ``?cat=`` page).  Calling :func:`categories` there made a cold category
    link fetch the 200 KB homepage as well; this uses the parse cache, or
    re-parses an already-cached homepage body, and otherwise returns empty.
    """
    record = cache.get(RTP_CATEGORIES_KEY, PACKS_TTL)
    if isinstance(record, dict):
        value = record.get("value")
        if isinstance(value, dict):
            return value
    page = http.cached_body(BASE, PACKS_TTL, PAGE_HEADERS)
    if not page:
        return dict(EMPTY_CATEGORIES)
    parsed = _cached_parse(RTP_CATEGORIES_KEY, PACKS_TTL, page, _build_categories)
    return parsed if isinstance(parsed, dict) else dict(EMPTY_CATEGORIES)


def _query_value(url, key):
    try:
        values = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    except ValueError:
        return ""
    return (values.get(key) or [""])[0]


def _next_category_url(page, current_url):
    """Return the WordPress "older posts" URL for this category page, or ``""``.

    WordPress themes spell the next link half a dozen ways (``link rel=next``,
    ``next page-numbers``, ``nextpostslink``, a ``nav-previous`` wrapper).  All
    of them are accepted, but the category id and a forward ``paged`` value are
    required, so the "Newer posts" link on the last page cannot walk backwards
    and a cross-category link cannot escape the archive.
    """
    current_cat = _query_value(current_url, "cat")
    try:
        current_page = max(1, int(_query_value(current_url, "paged") or 1))
    except (TypeError, ValueError):
        current_page = 1
    candidates = []
    for tag in _TAG_RE.findall(page or ""):
        href = _attr_value(tag, "href")
        if not href:
            continue
        rel = _attr_value(tag, "rel").lower().split()
        classes = _attr_value(tag, "class").lower().split()
        if ("next" in rel or "nextpostslink" in classes
                or ("next" in classes and "page-numbers" in classes)):
            candidates.append(href)
    for match in _ANCHOR_RE.finditer(page or ""):
        body = _tag_text(match.group(2)).lower()
        href = _attr_value(match.group(1), "href")
        if href and (body in ("next", "next page")
                     or "older posts" in body or "older entries" in body
                     or "next page" in body):
            candidates.append(href)
    ahead = []
    for match in _ANCHOR_RE.finditer(page or ""):
        href = _decode_url(_attr_value(match.group(1), "href"))
        found = re.search(r"[?&](?:amp;)?paged=(\d+)", href, re.IGNORECASE)
        if not found:
            continue
        try:
            number = int(found.group(1))
        except ValueError:
            continue
        if number > current_page:
            ahead.append((number, _attr_value(match.group(1), "href")))
    if ahead:
        ahead.sort(key=lambda item: item[0])
        candidates.append(ahead[0][1])
    for raw in candidates:
        url = urllib.parse.urljoin(BASE, _decode_url(raw))
        try:
            split = urllib.parse.urlsplit(url)
        except ValueError:
            continue
        if split.scheme.lower() not in ("http", "https") or not split.hostname:
            continue
        if not _allowed_host(split.hostname.lower(), BASE):
            continue
        if not current_cat or _query_value(url, "cat") != current_cat:
            continue
        try:
            page_number = int(_query_value(url, "paged") or 1)
        except (TypeError, ValueError):
            page_number = 1
        if page_number <= current_page:
            continue
        return url
    return ""


def _posts_from_html(page):
    """Parse a category archive's ``?p=<id>`` bookmark links, in page order."""
    posts, seen = [], set()
    for match in _ANCHOR_RE.finditer(page or ""):
        attrs, body = match.group(1), match.group(2)
        rel = _attr_value(attrs, "rel").lower().split()
        if "bookmark" not in rel or "<time" in body.lower():
            continue
        href = _decode_url(_attr_value(attrs, "href"))
        found = _POST_HREF_RE.search(href)
        if not found:
            continue
        post_id = found.group(1)
        if post_id in seen:
            continue
        name = _tag_text(body)
        if not name:
            continue
        seen.add(post_id)
        posts.append({"id": post_id, "name": name})
    return posts


def _packs_from_archive(page):
    """Parse a blog-archive page and keep only posts that carry audio.

    ``?cat=0`` also contains the site's sticky WELCOME/announcement posts.
    On this theme each post is one ``<article>``; a real pack embeds at least
    one ``.mp3`` player.  If the theme stops wrapping posts in ``<article>``
    (no chunks) fall back to the plain bookmark parse rather than returning
    nothing.
    """
    chunks = re.findall(r"<article\b[^>]*>.*?</article>", page or "",
                        re.IGNORECASE | re.DOTALL)
    if not chunks:
        return _posts_from_html(page)
    posts, seen = [], set()
    for chunk in chunks:
        # The sticky WELCOME post renders the "RANDOM SELECTION" slider, which
        # contains audio players of its own, so "has an MP3" cannot tell it
        # apart from a pack.  Its category/sticky marker can.
        opening = chunk[:chunk.find(">") + 1].lower()
        if "category-welcome" in opening or "format-status" in opening:
            continue
        if not re.search(r'(?:href|src)="[^"]+\.mp3(?:\?[^"]*)?"',
                         chunk, re.IGNORECASE):
            continue
        for post in _posts_from_html(chunk):
            if post["id"] in seen:
                continue
            seen.add(post["id"])
            posts.append(post)
    return posts


def _page_posts(url, key, parser=None):
    """Fetch one WordPress archive page; return ``(posts, next_url)``.

    ``(None, "")`` means the fetch itself failed (blocked/offline) so callers
    can tell "this category is empty" apart from "the source did not answer".
    While the homepage failure is still fresh, a child page answers from the
    negative cache instead of starting another 6 s (previously 25 s) request.
    """
    if homepage_failure_cached():
        return None, ""
    page = http.fetch(url, timeout=RTP_PAGE_TIMEOUT, ttl=CATEGORY_TTL,
                      headers=PAGE_HEADERS)
    if page is None:
        return None, ""
    posts = _cached_parse(key, CATEGORY_TTL, page, parser or _posts_from_html)
    if not isinstance(posts, list):
        posts = []
    return posts, _next_category_url(page, url)


def category_page(cat_id, page=1):
    """Return ``(posts, next_url)`` for one page of a RaveTapePacks category.

    A category is a normal WordPress archive: up to ten posts per page and an
    "Older posts" link.  Callers page through it instead of asking the source
    for every page in one request, which used to exceed the route budget and
    silently show only the first page or two.
    """
    cat_id = str(cat_id or "")
    if not cat_id.isdigit() or len(cat_id) > 12:
        return [], ""
    try:
        page = max(1, min(int(page), MAX_CATEGORY_PAGES))
    except (TypeError, ValueError):
        page = 1
    url = BASE + "?cat=" + cat_id
    if page > 1:
        url += "&paged=%d" % page
    return _page_posts(url, RTP_CATEGORY_PAGE_KEY % (cat_id, page))


def all_packs(page=1):
    """Return ``(posts, next_url)`` for one page of the complete pack archive.

    The homepage only renders a rotating "RANDOM SELECTION" block, so parsing
    it for an "All Packs" view showed a different handful of packs on every
    refresh.  ``?cat=0`` is the site's ordinary blog archive and lists every
    post in date order, paginated.
    """
    try:
        page = max(1, min(int(page), MAX_ALL_PACKS_PAGES))
    except (TypeError, ValueError):
        page = 1
    url = BASE + "?cat=0"
    if page > 1:
        url += "&paged=%d" % page
    return _page_posts(url, RTP_ALL_PACKS_KEY % page, _packs_from_archive)


def category(cat_id):
    """Return every pack (post) in one RaveTapePacks category, in page order.

    Kept as the aggregate helper (screen/other callers); HTTP routes use
    :func:`category_page` so one directory never spends the whole route budget
    crawling a long archive.
    """
    cat_id = str(cat_id or "")
    if not cat_id.isdigit() or len(cat_id) > 12:
        return []
    posts, seen_ids, seen_urls = [], set(), set()
    next_url = ""
    page = 1
    for _index in range(MAX_CATEGORY_PAGES):
        if page > 1:
            url = BASE + "?cat=" + cat_id + "&paged=%d" % page
        else:
            url = BASE + "?cat=" + cat_id
        if url in seen_urls:
            break
        seen_urls.add(url)
        batch, next_url = category_page(cat_id, page)
        if batch is None:
            break
        for post in batch:
            if post["id"] in seen_ids:
                continue
            seen_ids.add(post["id"])
            posts.append(post)
        if not next_url:
            break
        page += 1
        if page > MAX_CATEGORY_PAGES:
            break
    return posts



def _post_title(page):
    match = _ENTRY_TITLE_RE.search(page or "")
    if match:
        title = _tag_text(match.group(1))
        if title:
            return title
    match = re.search(r"<title[^>]*>(.*?)</title>", page or "",
                      re.IGNORECASE | re.DOTALL)
    if match:
        title = _tag_text(match.group(1))
        return re.split(r"\s+[|–-]\s+", title, 1)[0].strip()
    return ""


def _post_from_html(page, post_id):
    tracks, seen = [], set()
    for _pack, filename, url in _music_refs(page):
        if url in seen:
            continue
        seen.add(url)
        tracks.append({"name": _display(filename.rsplit(".", 1)[0]) or filename,
                       "url": url})
    return {"key": post_id,
            "name": _post_title(page) or ("Pack %s" % post_id),
            "tracks": tracks}


def post(post_id):
    """Return ``{'key', 'name', 'tracks'}`` for one RaveTapePacks post page.

    A post (``?p=899``) is the pack detail: it carries the direct MP3 players
    for that pack.  The resolver below is the same one the homepage index uses,
    so the Referer suffix and URL canonicalisation behave identically.
    """
    post_id = str(post_id or "")
    if not post_id.isdigit() or len(post_id) > 12:
        return None
    if homepage_failure_cached():
        return None
    page = http.fetch(BASE + "?p=" + post_id, timeout=RTP_PAGE_TIMEOUT,
                      ttl=POST_TTL, headers=PAGE_HEADERS)
    if page is None:
        return None
    entry = _cached_parse(
        RTP_POST_KEY % post_id, POST_TTL, page,
        lambda body: _post_from_html(body, post_id))
    return entry if isinstance(entry, dict) else None


def pack(key, kind=None, cat_id=None):
    """Resolve one homepage pack key (the "All Packs" view).

    Category links now open a post page, so ``kind``/``cat_id`` are accepted
    for older callers but no longer needed: a post's tracks come from
    :func:`post` instead.
    """
    key = str(key or "")
    if not key:
        return None
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
# A show page has two audio fields: "Stream the Jungle Tape Audio" (the
# digitised upload, one MP3 per side) and "Download Link(s)" (the same sides
# again as the original tape files plus OGG copies).  Parsing the whole page
# listed every recording twice; prefer the stream field and only fall back to
# the downloads field when a page has no stream player.
JT_STREAM_FIELD = "field-name-field-upload-audio-file"
JT_DOWNLOAD_FIELD = "field-name-field-audio-files"


def _fetch_jt_catalogue():
    """Fetch ``/tapes`` and ``/sitemap.xml`` concurrently; ``(index, xml)``.

    The two catalogue pages are independent and JungleTapes has no politeness
    gap configured, so a cold hub pays one round trip instead of two.  Any
    executor problem falls back to the old serial order.
    """
    urls = (JT_BASE + "/tapes", JT_BASE + "/sitemap.xml")

    def one(url):
        return http.fetch(url, timeout=30, ttl=SITEMAP_TTL)

    try:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(one, url) for url in urls]
            return futures[0].result(), futures[1].result()
    except Exception:  # noqa: BLE001 - serial is always correct, just slower
        return one(urls[0]), one(urls[1])


def jungle_shows():
    """Toronto jungle/DnB radio shows digitised from cassette — one page each.

    The site's front page only lists the newest two, so the sitemap is the
    catalogue: 300+ /show/ pages.
    """
    slugs = []
    # /tapes is the current catalogue (400+ entries); the sitemap catches
    # anything older that has dropped off it. Union both, then de-dupe below.
    index, xml = _fetch_jt_catalogue()
    if index:
        slugs.extend(re.findall(r'href="/show/([^"]+)"', index))
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


def _jt_audio_hrefs(page):
    """MP3 hrefs from a show's *stream* fields, else its download fields.

    A show page repeats the pair "stream player, download links" once per tape
    side, so every stream field must be collected (stopping at that side's
    download field) before falling back to the whole page.
    """
    text = page or ""
    hrefs = []
    start = text.find(JT_STREAM_FIELD)
    while start >= 0:
        end = text.find(JT_DOWNLOAD_FIELD, start)
        segment = text[start:end if end >= 0 else len(text)]
        hrefs.extend(JT_MP3_RE.findall(segment))
        if end < 0:
            break
        start = text.find(JT_STREAM_FIELD, end)
    return hrefs or JT_MP3_RE.findall(text)


def _jt_canonical(split):
    """Canonical (scheme, host, path) for one JungleTapes file URL.

    The site emits the same file as ``http://www.jungletapes.com/...``, as a
    relative path, and as ``https://jungletapes.com/...?uuid=...``.  Dropping
    the cache-busting ``uuid`` and the ``www``/``http`` variants is what makes
    the de-dupe key stable; other query parameters are preserved.
    """
    host = (split.hostname or "").lower()
    canonical = "jungletapes.com" if host == "www.jungletapes.com" else host
    query = [(key, value) for key, value in urllib.parse.parse_qsl(
        split.query, keep_blank_values=True) if key.lower() != "uuid"]
    return urllib.parse.urlunsplit(
        ("https", canonical, split.path, urllib.parse.urlencode(query), ""))


def jungle_tracks(slug):
    quoted = _url_path(slug)
    if not quoted:
        return []
    page = http.fetch(JT_BASE + "/show/" + quoted, timeout=25, ttl=SHOW_TTL)
    if not page:
        return []
    tracks, seen = [], set()
    for raw in _jt_audio_hrefs(page):
        # Decode exactly once (mirrors the add-on): the href may contain
        # ``&amp;`` for a real ampersand or a JSON ``\/`` escape.
        url = _decode_url(raw)
        if url.startswith("//"):
            url = "https:" + url
        url = urllib.parse.urljoin(JT_BASE + "/", url)
        split = urllib.parse.urlsplit(url)
        # The site emits absolute http:// links for its own host and
        # www.jungletapes.com 302s to the apex for every file.  Canonicalise
        # both here so the proxy does not follow a redirect per stream.
        host = (split.hostname or "").lower()
        if host.endswith("jungletapes.com"):
            url = _jt_canonical(split)
            split = urllib.parse.urlsplit(url)
        # Never pass a scraped file:/javascript:/data: URL to the browser.
        if split.scheme.lower() not in ("http", "https") or not split.netloc:
            continue
        key = _jt_canonical(split) if host.endswith("jungletapes.com") else url
        if key in seen:
            continue
        seen.add(key)
        filename = urllib.parse.unquote(url.rsplit("/", 1)[-1]).rsplit(".", 1)[0]
        tracks.append({"name": _display(filename), "url": url})
    return tracks
