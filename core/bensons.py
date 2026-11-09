# -*- coding: utf-8 -*-
"""20bensons.com — the old-skool rave MP3 wall.

The homepage is a long, hand-maintained list of 234 direct MP3 links under
``mp3/``.  There is no feed, playlist JSON or pagination, so this module parses
the anchor order from the page, decodes the filename into a title and derives
a year/event fact where the filename carries one.  The MP3s themselves are
plain Apache files with Range support; the app streams them through the shared
``/api/stream`` proxy and allows direct downloads from the one host.

The page is fetched with the normal cache and parsed once; a cold failure is a
genuinely empty room rather than an exception.
"""

import html
import re
import urllib.parse

from . import cache
from . import fetch as http

BASE = "https://20bensons.com/"
SOURCE_LABEL = "20bensons.com"
TTL = 6 * 60 * 60
PAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
HREF_RE = re.compile(r"\bhref\s*=\s*([\"'])(.*?)\1", re.IGNORECASE | re.DOTALL)
MP3_RE = re.compile(r"(?:^|/)(?:mp3/)([^?#]+\.mp3)(?:\?[^#]*)?$", re.IGNORECASE)
SOUR_RE = re.compile(r"[-_\s]+SOUR(?:\s*P?\d+)?\s*$", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(19[89]\d|20[0-2]\d)\b")
SHORT_YEAR_RE = re.compile(r"(?:'|\bNYE\s*|\bFOR\s+)(\d{2})\b", re.IGNORECASE)
DATE_YEAR_RE = re.compile(r"\b\d{1,2}[-/.]\d{1,2}[-/.](\d{2,4})\b")

# Most specific first: "Helter Skelter" must not also match a generic "rave".
EVENTS = (
    ("Helter Skelter", re.compile(r"helter\s*skelter", re.IGNORECASE)),
    ("Dreamscape", re.compile(r"dreamscape", re.IGNORECASE)),
    ("Fantazia", re.compile(r"fanta(?:z|s)ia", re.IGNORECASE)),
    ("Kings of the Jungle", re.compile(r"kings?\s+of\s+the\s+jungle", re.IGNORECASE)),
    ("Science of Rhythm", re.compile(r"science\s+of\s+rhythm", re.IGNORECASE)),
    ("Elevation", re.compile(r"elevation(?:\s+together)?(?:\s+for\s+\d+)?", re.IGNORECASE)),
    ("Telepathy", re.compile(r"telepathy", re.IGNORECASE)),
    ("Junglemania", re.compile(r"junglemania", re.IGNORECASE)),
    ("Raindance", re.compile(r"raindance", re.IGNORECASE)),
    ("Ravealation", re.compile(r"ravealation", re.IGNORECASE)),
    ("Future Myth", re.compile(r"future\s+myth", re.IGNORECASE)),
    ("Club X", re.compile(r"club\s+['\"]?x['\"]?", re.IGNORECASE)),
    ("Amnesia House", re.compile(r"amnesia(?:\s+house)?", re.IGNORECASE)),
    ("Sterns", re.compile(r"sterns", re.IGNORECASE)),
    ("Universe", re.compile(r"universe", re.IGNORECASE)),
    ("AWOL", re.compile(r"\ba\.?w\.?o\.?l\.?\b", re.IGNORECASE)),
    ("Quest", re.compile(r"\bquest\b", re.IGNORECASE)),
    ("Innovation", re.compile(r"innovation", re.IGNORECASE)),
    ("Don FM", re.compile(r"\bdon\s+105", re.IGNORECASE)),
    ("Energy FM", re.compile(r"energy\s+fm", re.IGNORECASE)),
)


def _attr(attrs, name):
    """One attribute value from an anchor tag, respecting its quote style."""
    match = re.search(r"\b%s\s*=\s*([\"'])(.*?)\1" % re.escape(name),
                      attrs or "", re.IGNORECASE | re.DOTALL)
    return match.group(2) if match else ""


def _norm_year(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""
    if len(str(value)) == 2:
        number = 1900 + number if number >= 60 else 2000 + number
    if 1987 <= number <= 2026:
        return str(number)
    return ""


def year_from(text):
    """Best-effort year from a filename: 1994, '94, NYE92, 13-08-93."""
    if not isinstance(text, str) or not text:
        return ""
    match = YEAR_RE.search(text)
    if match:
        return _norm_year(match.group(1))
    match = DATE_YEAR_RE.search(text)
    if match:
        value = match.group(1)
        if len(value) == 4:
            return _norm_year(value)
        return _norm_year(value)
    match = SHORT_YEAR_RE.search(text)
    if match:
        return _norm_year(match.group(1))
    return ""


def event_from(text):
    """A known rave/promoter name found in a filename, or ``""``."""
    if not isinstance(text, str) or not text:
        return ""
    searchable = text.replace("_", " ")
    for label, pattern in EVENTS:
        if pattern.search(searchable):
            return label
    return ""


def _clean_title(value):
    text = urllib.parse.unquote(str(value or ""))
    text = re.sub(r"\.mp3$", "", text, flags=re.IGNORECASE)
    text = text.replace("_", " ")
    text = SOUR_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -_.")


def _absolute(href):
    """Return a raw-space/quote-safe absolute MP3 URL, or ``""``.

    The page's own links are unescaped: they contain spaces and apostrophes,
    and one or two are missing their closing quote in the original markup.
    Decode once, reject a foreign host, then quote the path exactly once so a
    literal ``%20`` is not turned into ``%2520``.
    """
    text = html.unescape(str(href or "")).strip()
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    try:
        url = urllib.parse.urljoin(BASE, text)
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower().rstrip(".")
    if host != "20bensons.com":
        return ""
    path = urllib.parse.unquote(parts.path)
    if not path.lower().endswith(".mp3"):
        return ""
    path = urllib.parse.quote(path, safe="/!$&'()*+,;=:@-._~")
    return urllib.parse.urlunsplit(("https", "20bensons.com", path, "", ""))


def parse_homepage(text):
    """Parse every ``<a href="mp3/...">`` in document order.

    Duplicate links are kept: the page deliberately repeats a handful of
    featured sides near the top and bottom, and the owner's count is 234.
    """
    if not isinstance(text, str) or not text:
        return []
    rows = []
    for anchor in ANCHOR_RE.finditer(text):
        href = _attr(anchor.group(1), "href")
        if not href or "mp3/" not in href.lower():
            continue
        url = _absolute(href)
        if not url:
            continue
        filename = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
        if not filename:
            continue
        title = _clean_title(filename)
        if not title:
            continue
        rows.append({
            "url": url,
            "filename": filename,
            "title": title,
            "year": year_from(filename),
            "event": event_from(filename),
        })
    return rows


def _load_entries():
    body = http.fetch(BASE, timeout=25, headers=PAGE_HEADERS, ttl=TTL)
    return parse_homepage(body) if body else None


def entries():
    """All 234 rows, parsed once from the cached homepage (never raises).

    A cold or slow upstream must not empty the room: once a good list exists,
    an expired cache is served stale while the next refresh runs.
    """
    key = "bensons:entries:v1"
    value = cache.get(key, TTL)
    if value is not cache.MISSING:
        return value
    loaded = _load_entries()
    if loaded is None:
        stale = cache.get(key, 10 ** 9)
        return [] if stale is cache.MISSING else stale
    cache.put(key, loaded, sync=False)
    return loaded


def summary(rows):
    """Short room subtitle: count plus the years the page actually covers."""
    rows = rows if isinstance(rows, list) else []
    years = sorted({row.get("year") for row in rows if row.get("year")})
    if not rows:
        return "Source unavailable — try again later"
    pieces = ["%d old-skool rave MP3s" % len(rows)]
    if years:
        pieces.append(years[0] if len(years) == 1 else "%s–%s" % (years[0], years[-1]))
    pieces.append(SOURCE_LABEL)
    return " · ".join(pieces)
