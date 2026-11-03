# -*- coding: utf-8 -*-
"""Toronto Rave Mixtape Archive — direct-MP3 genre pages.

The archive has no API.  Its nav exposes four genre pages:

    /jungle.html  /house.html  /studio.html  /xstatic.html

Each page is a list of direct MP3s (``/files/mixtape/...`` or
``/files/xstatic/...``) with a title in the ``download`` attribute and a
duration/bitrate/size line.  There are no platform pages to hand off: tracks
play through the normal same-origin ``/api/stream`` proxy and can be saved via
the existing direct-file download path.

Requests are spaced to <= 1/s and cached for 6 h; parsing is separated from
fetching so tests use saved fixtures and never touch the network.
"""

import html
import re
import threading
import time
import urllib.parse

from . import fetch

BASE = "https://torontoravemixtapearchive.com"
PAGE_TTL = 6 * 3600
MIN_INTERVAL = 1.0
DEFAULT_LIMIT = None

GENRES = (
    ("jungle", "Jungle & Hardcore", "jungle.html"),
    ("house", "House & Techno", "house.html"),
    ("studio", "Studio", "studio.html"),
    ("xstatic", "X-Static", "xstatic.html"),
)
GENRE_BY_ID = {key: {"id": key, "label": label, "page": page}
               for key, label, page in GENRES}

_ANCHOR_RE = re.compile(r"<a\b(?P<tag>[^>]*)>(?P<body>.*?)</a>", re.I | re.S)
_CLASS_RE = re.compile(r'class="([^"]*)"', re.I)
_HREF_RE = re.compile(r'href="([^"]*)"', re.I)
_DOWNLOAD_RE = re.compile(r'download="([^"]*)"', re.I)
_HEADER_RE = re.compile(r'class="header"[^>]*>(.*?)</div>', re.I | re.S)
_META_RE = re.compile(r'class="metadata"[^>]*>(.*?)</div>', re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_DURATION_RE = re.compile(r"\b(\d{1,3}:\d{2}(?::\d{2})?)\b")
_BITRATE_RE = re.compile(r"(\d{2,3})\s*kbps", re.I)
_SIZE_RE = re.compile(r"([\d.]+)\s*(MB|GB)", re.I)
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_DATE_RE = re.compile(
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True))
    + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?[,\s]+(\d{2,4})\b", re.I)
_NUM_DATE_RE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b")
_SPACED_DATE_RE = re.compile(r"\b(\d{1,2})\s+(\d{1,2})\s+(\d{2,4})\b")
_YEAR_RE = re.compile(r"\b(19[89]\d|20[0-2]\d)\b")
_LIVE_RE = re.compile(r"\bLive\s+(?:at|@)\s+(.+)", re.I)
_TRAILING_DATE_RE = re.compile(
    r"[\[(]?\s*(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True))
    + r")\.?\s+\d{1,2}(?:st|nd|rd|th)?[,\s]+\d{2,4}\s*[\])]?", re.I)
_TRAILING_NUM_DATE_RE = re.compile(r"\s*[\[(]?\d{1,2}[./-]\d{1,2}[./-]\d{2,4}[\])]?\s*$")

_THROTTLE = {"at": 0.0}
_THROTTLE_LOCK = threading.Lock()


def clear_memory():
    """Kept for the test cache fixture; fetch/cache owns the real cache."""
    return None


def _throttle():
    with _THROTTLE_LOCK:
        wait = MIN_INTERVAL - (time.monotonic() - _THROTTLE["at"])
        if wait > 0:
            time.sleep(wait)
        _THROTTLE["at"] = time.monotonic()


def _get(url):
    _throttle()
    return fetch.fetch(url, timeout=25, ttl=PAGE_TTL)


def _clean(value, limit=300):
    text = html.unescape(str(value or ""))
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _attr(tag, pattern):
    match = pattern.search(tag)
    return html.unescape(match.group(1)).strip() if match else ""


def _year(raw):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    if value < 100:
        value += 2000 if value < 30 else 1900
    return value


def _valid_date(year, month, day):
    if not (1980 <= year <= 2030 and 1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return "%04d-%02d-%02d" % (year, month, day)


def date_and_venue(title):
    """Best-effort ISO date + venue/event from a human mixtape title."""
    text = _clean(title)
    date = ""
    match = _MONTH_DATE_RE.search(text)
    if match:
        month = _MONTHS.get(match.group(1).lower().rstrip("."), 0)
        date = _valid_date(_year(match.group(3)), month, int(match.group(2)))
    if not date:
        match = _NUM_DATE_RE.search(text)
        if match:
            first, second, raw_year = int(match.group(1)), int(match.group(2)), match.group(3)
            year = _year(raw_year)
            # 02-11-2006 is month-day on this site; >12 in the first slot is
            # day-month.  Invalid combinations fall back to day-month.
            if first > 12:
                date = _valid_date(year, second, first)
            else:
                date = _valid_date(year, first, second) or _valid_date(year, second, first)
    if not date:
        match = _SPACED_DATE_RE.search(text)
        if match:
            date = _valid_date(_year(match.group(3)), int(match.group(2)), int(match.group(1)))
    if not date:
        match = _YEAR_RE.search(text)
        if match:
            date = match.group(1)
    venue = ""
    match = _LIVE_RE.search(text)
    if match:
        venue = match.group(1)
        # Stop at the date separator or date itself; keep venue words intact.
        venue = re.split(r"\s+[-–—]\s+", venue, 1)[0]
        venue = _TRAILING_DATE_RE.sub("", venue)
        venue = _TRAILING_NUM_DATE_RE.sub("", venue)
        venue = re.sub(r"\s+[-–—]\s*$", "", venue).strip(" ,-–—")
        venue = _clean(venue, 120)
    return date, venue


def parse_page(page):
    """Parse one genre page into title/date/venue/url/metadata rows."""
    if not isinstance(page, str) or not page.strip():
        return []
    rows, seen = [], set()
    for match in _ANCHOR_RE.finditer(page):
        tag = match.group("tag")
        classes = _attr(tag, _CLASS_RE)
        if "track-dl" not in classes.split():
            continue
        href = _attr(tag, _HREF_RE)
        if not href:
            continue
        path = urllib.parse.urlsplit(href).path
        if not path.startswith("/files/") or not path.lower().endswith(".mp3"):
            continue
        url = urllib.parse.urljoin(BASE + "/", href)
        # The site's hrefs contain raw spaces; /api/stream rejects whitespace,
        # so emit a valid encoded URL that the proxy/download path can use.
        url = urllib.parse.quote(url, safe=":/?&=%#[]@!$&'()*+,;=-._~")
        if url in seen:
            continue
        body = match.group("body")
        title = _attr(tag, _DOWNLOAD_RE)
        if not title:
            header = _HEADER_RE.search(body)
            title = _clean(header.group(1)) if header else ""
        title = _clean(title)
        if not title:
            title = _clean(urllib.parse.unquote(path.rsplit("/", 1)[-1])[:-4]) or "Toronto mixtape"
        meta = _META_RE.search(body)
        metadata = _clean(meta.group(1), 120) if meta else ""
        duration = bitrate = ""
        size = 0
        if metadata:
            duration_match = _DURATION_RE.search(metadata)
            bitrate_match = _BITRATE_RE.search(metadata)
            size_match = _SIZE_RE.search(metadata)
            duration = duration_match.group(1) if duration_match else ""
            bitrate = bitrate_match.group(1) if bitrate_match else ""
            if size_match:
                size = float(size_match.group(1)) * (1024 if size_match.group(2).upper() == "GB" else 1)
        date, venue = date_and_venue(title)
        rows.append({
            "title": title,
            "date": date,
            "venue": venue,
            "url": url,
            "duration": duration,
            "bitrate": bitrate,
            "size_mb": size,
            "metadata": metadata,
        })
        seen.add(url)
    return rows


def _genre_rows(genre):
    entry = GENRE_BY_ID.get(str(genre or "").lower())
    if not entry:
        return []
    try:
        page = _get(BASE + "/" + entry["page"])
    except Exception:  # noqa: BLE001 - one dead page must not break the hub
        return []
    return parse_page(page)


def genre_items(genre, limit=None):
    rows = _genre_rows(genre)
    if limit is None:
        return rows
    try:
        cap = max(1, min(int(limit), 5000))
    except (TypeError, ValueError):
        cap = None
    return rows if cap is None else rows[:cap]


def search(query, limit=None):
    """Filter titles/venues across every genre (network only on a cold call)."""
    needle = _clean(query, 100).lower()
    if not needle:
        return []
    cap = None
    if limit is not None:
        try:
            cap = max(1, min(int(limit), 5000))
        except (TypeError, ValueError):
            cap = None
    found = []
    for genre, _label, _page in GENRES:
        for entry in _genre_rows(genre):
            haystack = " ".join((entry.get("title", ""), entry.get("venue", ""),
                                 entry.get("date", ""))).lower()
            if needle in haystack:
                found.append(entry)
                if cap is not None and len(found) >= cap:
                    return found
    return found


def genres():
    return [dict(entry) for entry in GENRE_BY_ID.values()]
