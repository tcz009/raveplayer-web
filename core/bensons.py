# -*- coding: utf-8 -*-
"""20bensons.com — the old-skool rave MP3 wall.

The homepage is a long, hand-maintained list of 234 direct MP3 links under
``mp3/``.  There is no feed, playlist JSON or pagination, so this module parses
the anchor order from the page, decodes the filename into a title and derives
a year/event fact where the filename carries one.  The MP3s themselves are
plain Apache files with Range support; the app streams them through the shared
``/api/stream`` proxy and allows direct downloads from the one host.

The page is fetched with the normal cache and parsed once.  The parsed list is
served from cache even after it technically expires (stale-while-revalidate):
when it reaches ~70% of its TTL one background thread refreshes it, so a
visitor never pays for the upstream homepage scrape.  A cold failure is a
genuinely empty room rather than an exception, and a failed refresh always
leaves the previous list in place.
"""

import html
import re
import threading
import time
import urllib.parse

from . import cache
from . import fetch as http
from . import kodi

BASE = "https://20bensons.com/"
SOURCE_LABEL = "20bensons.com"
CACHE_KEY = "bensons:entries:v1"
# The homepage changes slowly; 24h of cache is plenty when both a soft refresh
# and a hard stale fallback sit in front of it.
TTL = 24 * 60 * 60
# Serve the cache immediately once it is this old and refresh it in the
# background.  Together with the stale fallback below this means a request
# only ever waits for upstream when the cache has never existed.
REVALIDATE_AFTER = int(TTL * 0.7)
# After a failed/empty refresh, stay away from the upstream for a while: the
# stale entry is still being served, and an offline NAS must not turn every
# room open into another 25-second scrape and a fresh warning line.
REFRESH_RETRY_SECONDS = 5 * 60
_NEVER = 10 ** 9

# Process-local scheduling state.  ``_LOAD_LOCK`` single-flights the actual
# upstream load (a cold request and a background warm cannot scrape twice),
# while ``_REFRESH_LOCK`` only guards the small state transition so a request
# never blocks on a refresh.  No Flask/app context is touched here: the warm
# hook and the worker run happily outside a request.
_LOAD_LOCK = threading.Lock()
_REFRESH_LOCK = threading.Lock()
_REFRESH_ACTIVE = False
_REFRESH_LAST_ATTEMPT = 0.0
_REFRESH_DONE = threading.Event()
_REFRESH_DONE.set()
_WARM_LOCK = threading.Lock()
_WARMED = False

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


def _load_entries(force=False):
    """Fetch and parse the homepage, or return ``None`` on failure.

    ``force`` bypasses the HTTP body cache so a stale-while-revalidate pass
    actually revalidates against upstream; a cold miss can still reuse a fresh
    cached body (for example when only the parsed entry was lost).
    """
    body = http.fetch(BASE, timeout=25, headers=PAGE_HEADERS,
                      ttl=0 if force else TTL)
    return parse_homepage(body) if body else None


def _publish(loaded):
    """Replace the parsed cache only with a usable, non-empty list.

    The cache write is best effort: a read-only profile or full disk must not
    turn a successfully parsed page into an empty room.
    """
    if not loaded:
        return False
    cache.put(CACHE_KEY, loaded, sync=False)
    return True


def _refresh_worker():
    """One upstream revalidation; never raises and never touches a request."""
    started = time.time()
    try:
        with _LOAD_LOCK:
            # Another thread/process may have refreshed while this worker was
            # queued; in that case there is nothing left to do.
            age = cache.age(CACHE_KEY)
            if age is not None and age < REVALIDATE_AFTER:
                return
            loaded = _load_entries(force=True)
            if _publish(loaded):
                kodi.log("bensons: refreshed %d entries in %.2fs"
                         % (len(loaded), time.time() - started), level=10)
            else:
                # fetch() has already logged a transport/http warning.  Keep
                # the previous list and stay quiet until the retry cooldown.
                kodi.log("bensons: refresh had no entries; keeping stale cache",
                         level=20)
    except Exception:  # noqa: BLE001 - a background refresh must never raise
        kodi.log("bensons: refresh failed; keeping stale cache", level=10)
    finally:
        global _REFRESH_ACTIVE
        with _REFRESH_LOCK:
            _REFRESH_ACTIVE = False
            _REFRESH_DONE.set()


def _schedule_refresh():
    """Start the single background refresh, or return False if one is due later.

    Deduped two ways: an active worker suppresses every other request, and a
    short cooldown after an attempt keeps an offline/failing upstream from
    being retried on every page load.  The caller never joins the thread.
    """
    global _REFRESH_ACTIVE, _REFRESH_LAST_ATTEMPT
    now = time.time()
    with _REFRESH_LOCK:
        if _REFRESH_ACTIVE:
            return False
        if (_REFRESH_LAST_ATTEMPT
                and now - _REFRESH_LAST_ATTEMPT < REFRESH_RETRY_SECONDS):
            return False
        _REFRESH_ACTIVE = True
        _REFRESH_LAST_ATTEMPT = now
        _REFRESH_DONE.clear()
    try:
        thread = threading.Thread(target=_refresh_worker,
                                  name="bensons-refresh", daemon=True)
        thread.start()
    except Exception:  # pragma: no cover - thread creation unavailable
        with _REFRESH_LOCK:
            _REFRESH_ACTIVE = False
        _REFRESH_DONE.set()
        return False
    return True


def _maybe_refresh():
    """Schedule a refresh once the cached list is old enough; never waits."""
    age = cache.age(CACHE_KEY)
    if age is not None and age >= REVALIDATE_AFTER:
        return _schedule_refresh()
    return False


def warm():
    """Warm the cache in the background, once per process.

    Called from a request hook so a deploy/restart does not leave the NAS
    cold.  It only inspects a timestamp and (at most) starts one daemon
    thread, so the request itself is never delayed and an offline upstream
    produces one failed attempt rather than a per-request retry loop.  The
    flag is consumed even when the cache is already fresh: there is nothing
    for a second call in this process to do.
    """
    global _WARMED
    with _WARM_LOCK:
        if _WARMED:
            return False
        _WARMED = True
    age = cache.age(CACHE_KEY)
    if age is None or age >= REVALIDATE_AFTER:
        return _schedule_refresh()
    return False


def _reset_state():
    """Reset process-local scheduling state (tests and local benchmarks only)."""
    global _REFRESH_ACTIVE, _REFRESH_LAST_ATTEMPT, _WARMED
    with _REFRESH_LOCK:
        _REFRESH_ACTIVE = False
        _REFRESH_LAST_ATTEMPT = 0.0
        _REFRESH_DONE.set()
    with _WARM_LOCK:
        _WARMED = False


def entries():
    """All 234 rows from the cached homepage (never raises).

    The cached list is returned immediately whenever it exists, however old it
    is.  A list past :data:`REVALIDATE_AFTER` additionally queues one
    background refresh; a truly cold cache loads synchronously, single-flighted
    with any warm already in progress.  A failed load leaves a previous list
    untouched and, on a first-ever failure, returns an empty room.
    """
    value = cache.get(CACHE_KEY, _NEVER)
    if value is not cache.MISSING:
        _maybe_refresh()
        return value

    with _LOAD_LOCK:
        # A warm or another cold request may have filled the cache while we
        # waited for the load lock.
        value = cache.get(CACHE_KEY, _NEVER)
        if value is not cache.MISSING:
            _maybe_refresh()
            return value
        loaded = _load_entries()
        if _publish(loaded):
            return loaded
        stale = cache.get(CACHE_KEY, _NEVER)
        return [] if stale is cache.MISSING else stale


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
