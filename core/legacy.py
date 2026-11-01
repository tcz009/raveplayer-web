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

from . import fetch as http

BASE = "https://www.ravetapepacks.com/"
REFERER_SUFFIX = "|Referer=https://www.ravetapepacks.com/&User-Agent=Mozilla/5.0"

PACKS_TTL = 6 * 60 * 60
SITEMAP_TTL = 24 * 60 * 60
SHOW_TTL = 6 * 60 * 60
TORONTO_TTL = 6 * 60 * 60

MUSIC_RE = re.compile(
    r"(?:https?://www\.ravetapepacks\.com)?/music/([^/\"'<>]+)/([^\"'<>]+?\.mp3)",
    re.IGNORECASE,
)


def _space_words(value):
    """Unquote, unescape and split camelCase/letter-digit joins."""
    text = html.unescape(urllib.parse.unquote(str(value or "")))
    text = text.replace("_", " ")
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[0-9])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Za-z])(?=[0-9])", " ", text)
    return re.sub(r"\s+", " ", text).strip()


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
    return _space_words(text)


def _pack_name(raw):
    """Folder names are camel-cased and run words together; space them out."""
    return _space_words(raw) or str(raw)


def packs():
    """Return [{'key': folder, 'name': label, 'tracks': [...]}, ...] sorted by name."""
    page = http.fetch(BASE, timeout=25, ttl=PACKS_TTL)
    if not page:
        return []
    grouped = {}
    seen = set()
    for pack, filename in MUSIC_RE.findall(page):
        pack = html.unescape(pack)
        filename = html.unescape(filename)
        pack_path, file_path = _url_path(pack), _url_path(filename)
        if not pack_path or not file_path:
            continue
        url = BASE + "music/" + pack_path + "/" + file_path
        if url in seen:
            continue
        seen.add(url)
        entry = grouped.setdefault(pack, {"key": pack, "name": _pack_name(pack), "tracks": []})
        entry["tracks"].append({"name": _pretty(filename.rsplit(".", 1)[0]), "url": url})
    result = []
    for entry in grouped.values():
        entry["tracks"].sort(key=lambda track: track["name"])
        result.append(entry)
    result.sort(key=lambda entry: entry["name"].lower())
    return result


def playable(url):
    """Kodi header syntax: appends Referer so the hotlink check passes."""
    text = str(url)
    if "|" in text:
        # Already has a header suffix (e.g. a saved favourite); never double it.
        return text
    return text + REFERER_SUFFIX


# --------------------------------------------------------------------------- jungletapes.com

JT_BASE = "https://jungletapes.com"
JT_SHOW_RE = re.compile(r'href="/show/([^"]+)"[^>]*>.*?</a>', re.IGNORECASE | re.DOTALL)
JT_MP3_RE = re.compile(r'href="([^"]+\.mp3)"', re.IGNORECASE)


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
    page = http.fetch(JT_BASE + "/show/" + _url_path(slug), timeout=25, ttl=SHOW_TTL)
    if not page:
        return []
    tracks = []
    for url in JT_MP3_RE.findall(page):
        url = html.unescape(url)
        if url.startswith("//"):
            url = "https:" + url
        url = urllib.parse.urljoin(JT_BASE + "/", url)
        filename = urllib.parse.unquote(url.rsplit("/", 1)[-1]).rsplit(".", 1)[0]
        tracks.append({"name": _pretty(filename), "url": url})
    return tracks


# --------------------------------------------------------------------------- torontoravemixtapearchive.com

TR_BASE = "https://www.torontoravemixtapearchive.com"
TR_MP3_RE = re.compile(r'href="(/files/mixtapes?/[^"]+\.mp3)"', re.IGNORECASE)


def toronto_tracks():
    """Flat index of Toronto rave mixtapes."""
    page = http.fetch(TR_BASE + "/", timeout=25, ttl=TORONTO_TTL)
    if not page:
        return []
    tracks = []
    for path in TR_MP3_RE.findall(page):
        filename = html.unescape(urllib.parse.unquote(path.rsplit("/", 1)[-1])).rsplit(".", 1)[0]
        tracks.append({"name": _pretty(filename), "url": TR_BASE + _url_path(path)})
    return tracks
