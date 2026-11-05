# -*- coding: utf-8 -*-
"""Moxser — weekly underground dance-music radio mixes.

Moxser publishes no API; the reliable route is its sitemap index:

    /sitemaps-1-sitemap.xml  (index)
      -> ...-section-mixes-1-sitemap-pN.xml  (one <loc> per mix page)
        -> /mixes/<slug>  (h1 title+date, flyer, and a JS config that points at
                           a direct MP3 on enceladus.feralhosting.com)

The MP3 host is public HTTPS with Range support, so the browser plays it via
the normal same-origin ``/api/stream`` proxy (no CSP or SSRF change needed:
the proxy already accepts any *public* http(s) host and strips private
addresses).

Requests are throttled to at most one per second and cached aggressively
(sitemaps 1 h, mix pages 6 h); every failure is soft — the section renders
empty rather than 500.
"""

import html
import re
import threading
import time
import urllib.parse

from . import fetch, sources

SITE = "https://moxser.com"
SITEMAP_INDEX = SITE + "/sitemaps-1-sitemap.xml"
MIX_PREFIX = SITE + "/mixes/"
SITEMAP_TTL = 3600
PAGE_TTL = 6 * 3600
MIN_INTERVAL = 1.0

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_LASTMOD = re.compile(r"<lastmod>([^<]+)</lastmod>", re.IGNORECASE)
_IMAGE = re.compile(r"<image:loc>\s*([^<\s]+)\s*</image:loc>", re.IGNORECASE)
_IMAGE_TITLE = re.compile(r"<image:title>\s*([^<\s]+)\s*</image:title>", re.IGNORECASE)
_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_MP3_PATH = re.compile(r"path:\s*['\"]([^'\"]+\.mp3[^'\"]*)['\"]", re.IGNORECASE)
_DATE_IN_TITLE = re.compile(r"\(([0-9]{1,2}\s+[A-Za-z]+\s+[0-9]{4})\)")

_THROTTLE = {"at": 0.0}
_THROTTLE_LOCK = threading.Lock()

# In-process copy of the parsed sitemap entries.  Without it every section
# load re-read the cached sitemaps, re-parsed 500+ URLs and still paid the
# 1 req/s throttle; this is the difference between a warm /api/moxser costing
# ~3 s and ~0.2 ms.  The TTL is short and the disk/fetch caches remain beneath.
ENTRIES_TTL = 600
_ENTRIES = {"at": 0.0, "entries": [], "complete": False}
_ENTRIES_LOCK = threading.Lock()


def clear_memory():
    with _ENTRIES_LOCK:
        _ENTRIES.update({"at": 0.0, "entries": [], "complete": False})


def _throttle():
    """Space out Moxser requests to <= 1/s."""
    with _THROTTLE_LOCK:
        now = time.monotonic()
        wait = MIN_INTERVAL - (now - _THROTTLE["at"])
        if wait > 0:
            time.sleep(wait)
        _THROTTLE["at"] = time.monotonic()


def _get(url, ttl):
    # A cached sitemap/page must not pay the <=1/s courtesy sleep.
    if not fetch.is_cached(url, ttl):
        _throttle()
    return fetch.fetch(url, timeout=25, ttl=ttl)


def _slug_from_url(url):
    path = urllib.parse.urlsplit(url).path
    if not path.startswith("/mixes/"):
        return ""
    return urllib.parse.unquote(path[len("/mixes/"):]).strip("/")


def _pretty_slug(slug):
    text = urllib.parse.unquote(str(slug or "")).replace("-", " ").replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1].upper() + text[1:] if text else ""


def _decode(value):
    """Decode HTML entities, including titles a CMS encoded twice.

    Scrapers see ``&amp;`` in a text node and ``&amp;#x2013;`` when the source
    encoded the entity itself.  Loop until stable (bounded) so the rendered
    title carries ``&`` and ``–``, never the entity source.
    """
    text = str(value or "")
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text


def _pretty_title(value):
    text = _decode(urllib.parse.unquote_plus(str(value or "")))
    return re.sub(r"\s+", " ", text).strip()


def _title_text(value):
    text = _decode(str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _date_hint(title, lastmod):
    match = _DATE_IN_TITLE.search(title or "")
    if match:
        return match.group(1)
    return (lastmod or "")[:10]


def _sitemap_urls():
    body = _get(SITEMAP_INDEX, SITEMAP_TTL)
    if not body:
        return []
    urls = []
    for match in _LOC.finditer(body):
        url = html.unescape(match.group(1))
        if "section-mixes-" in url and url not in urls:
            urls.append(url)
    return urls


def _sitemap_entries(body):
    entries = []
    for chunk in body.split("<url>")[1:]:
        loc = _LOC.search(chunk)
        if not loc:
            continue
        url = html.unescape(loc.group(1))
        slug = _slug_from_url(url)
        if not slug:
            continue
        title_match = _IMAGE_TITLE.search(chunk)
        title = _pretty_title(title_match.group(1)) if title_match else ""
        if not title:
            image = _IMAGE.search(chunk)
            if image:
                title = _pretty_slug(urllib.parse.urlsplit(html.unescape(image.group(1))).path.rsplit("/", 1)[-1].rsplit(".", 1)[0])
        title = title or _pretty_slug(slug)
        lastmod_match = _LASTMOD.search(chunk)
        lastmod = lastmod_match.group(1).strip() if lastmod_match else ""
        image = _IMAGE.search(chunk)
        entries.append({
            "slug": slug,
            "url": url,
            "title": title,
            "date": _date_hint(title, lastmod),
            "flyer": html.unescape(image.group(1)) if image else "",
            "lastmod": lastmod,
        })
    return entries


def _load_entries(limit, sitemap_urls=None):
    """Fetch mixes sitemaps until ``limit`` entries exist (or all are read)."""
    urls = sitemap_urls if sitemap_urls is not None else _sitemap_urls()
    entries = []
    complete = True
    for position, sitemap in enumerate(urls):
        body = _get(sitemap, SITEMAP_TTL)
        if body:
            entries.extend(_sitemap_entries(body))
        if len(entries) >= limit:
            complete = position == len(urls) - 1
            break
    deduped = {}
    for entry in entries:
        deduped.setdefault(entry["url"], entry)
    ordered = sorted(deduped.values(),
                     key=lambda entry: (entry.get("lastmod") or "", entry.get("slug") or ""),
                     reverse=True)
    return ordered, complete


def mixes(limit=None):
    """Newest mixes first, from the mixes sitemaps.  Fail-soft: [] on error.

    ``limit=None`` returns the complete mixes sitemap set (1.6.42); an explicit
    limit is honoured for internal callers/tests but is never imposed by the
    API.  Sources are sitemap-bounded, so there is no hidden 48/200 cap.
    """
    cap = None
    if limit is not None:
        try:
            cap = max(1, min(int(limit), 5000))
        except (TypeError, ValueError):
            cap = None
    try:
        now = time.monotonic()
        with _ENTRIES_LOCK:
            cached = list(_ENTRIES["entries"])
            fresh = (now - _ENTRIES["at"]) < ENTRIES_TTL
            complete = _ENTRIES["complete"]
        if fresh and complete:
            return cached if cap is None else cached[:cap]
        if cap is not None and fresh and len(cached) >= cap:
            return cached[:cap]
        if cap is None:
            merged = _all_entries()
            return merged
        urls = _sitemap_urls()
        ordered, complete = _load_entries(cap, urls)
        if not ordered and not complete:
            return []
        with _ENTRIES_LOCK:
            known = {entry["url"]: entry for entry in cached}
            known.update({entry["url"]: entry for entry in ordered})
            merged = sorted(known.values(),
                            key=lambda entry: (entry.get("lastmod") or "",
                                               entry.get("slug") or ""),
                            reverse=True)
            _ENTRIES.update({"at": time.monotonic(), "entries": merged,
                             "complete": complete})
        return merged[:cap]
    except Exception:  # noqa: BLE001 - one dead source must not break the section
        return []


def _all_entries():
    """Load every mixes sitemap (complete-list and detail fallback)."""
    now = time.monotonic()
    with _ENTRIES_LOCK:
        if (_ENTRIES["complete"]
                and (now - _ENTRIES["at"]) < ENTRIES_TTL):
            return list(_ENTRIES["entries"])
    urls = _sitemap_urls()
    ordered, _complete = _load_entries(10 ** 9, urls)
    with _ENTRIES_LOCK:
        known = {entry["url"]: entry for entry in _ENTRIES["entries"]}
        known.update({entry["url"]: entry for entry in ordered})
        _ENTRIES.update({
            "at": time.monotonic(),
            "entries": sorted(known.values(),
                              key=lambda entry: (entry.get("lastmod") or "",
                                                 entry.get("slug") or ""),
                              reverse=True),
            "complete": True,
        })
        return list(_ENTRIES["entries"])


def _known_entry(slug):
    """Look ``slug`` up in the in-process entries only: (entry, complete)."""
    now = time.monotonic()
    with _ENTRIES_LOCK:
        fresh = (now - _ENTRIES["at"]) < ENTRIES_TTL
        complete = fresh and _ENTRIES["complete"]
        entries = list(_ENTRIES["entries"]) if fresh else []
    return next((item for item in entries if item["slug"] == slug), None), complete


def _slug_ok(wanted):
    """Cheap sanity check for a slug used to build a moxser.com page URL."""
    if not wanted or len(wanted) > 200 or "\x00" in wanted:
        return False
    segments = wanted.split("/")
    if any(part in ("", ".", "..") for part in segments):
        return False
    return bool(re.match(r"^[A-Za-z0-9._~%+!$&'()*+,;=:@/-]+$", wanted))


def detail(slug):
    """Resolve one mix page to {title, url (MP3), date, flyer}; None on failure.

    A cold resolve used to build the whole sitemap entry list first (two
    rate-limited requests) just to recover the page URL, which is derivable
    from the slug.  The known-entry path is still preferred when the list is
    already loaded; otherwise one page fetch answers the click.
    """
    wanted = urllib.parse.unquote(str(slug or "")).strip("/")
    if not _slug_ok(wanted):
        return None
    try:
        entry, complete = _known_entry(wanted)
        if entry is None:
            if complete:
                return None  # the full sitemap is loaded and lacks this slug
            # Cold or stale list: the page URL is deterministic, so do not load
            # the sitemaps just to look up where the page lives.
            entry = {"slug": wanted,
                     "url": MIX_PREFIX + urllib.parse.quote(wanted, safe="/"),
                     "title": _pretty_slug(wanted), "date": "", "flyer": ""}
        body = _get(entry["url"], PAGE_TTL)
        if not body:
            return None
        title_match = _H1.search(body)
        title = _title_text(title_match.group(1)) if title_match else entry["title"]
        title = title or entry["title"]
        mp3 = ""
        path_match = _MP3_PATH.search(body)
        if path_match:
            mp3 = html.unescape(path_match.group(1))
        if not mp3:
            found = sources.media_urls(body)
            mp3 = found[0] if found else ""
        if not mp3.startswith("https://"):
            return None
        return {
            "slug": wanted,
            "title": title,
            "url": mp3,
            "date": _date_hint(title, entry.get("date")),
            "flyer": entry.get("flyer") or "",
        }
    except Exception:  # noqa: BLE001 - fail-soft on a third-party page
        return None
