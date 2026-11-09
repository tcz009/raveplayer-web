# -*- coding: utf-8 -*-
"""Demodulated Mixtapes — a WordPress feed of Toronto techno/rave tapes.

Each post carries the full mix sides as direct MP3s under
``www.demodulated.com/music/mixsets/`` plus a short sample and sometimes a ZIP.
The room lists posts; the detail lists only the MP3s, marks files whose name
contains ``sample`` and never puts the ZIP in the player.  The feed body and
the parsed posts are cached; the MP3s themselves stream through
``/api/stream`` and download from the one allowlisted host.
"""

import html
import re
import urllib.parse
import xml.etree.ElementTree as ET

from . import cache
from . import fetch as http
from .sources import clean_text, media_urls, parse_date

FEED_URL = "http://mixtapes.demodulated.com/feed/"
SOURCE_URL = "http://mixtapes.demodulated.com/"
SOURCE_LABEL = "Demodulated Mixtapes"
TTL = 6 * 60 * 60
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"
ZIP_RE = re.compile(r"https?://[^\s\"'<>]+?\.zip(?:\?[^\s\"'<>]*)?", re.IGNORECASE)
SAMPLE_RE = re.compile(r"\bsample\s*\d*\b", re.IGNORECASE)
SIDE_RE = re.compile(r"\bside[\s._-]*([a-d])\b", re.IGNORECASE)


def _text(node, tag):
    found = node.find(tag)
    return (found.text or "").strip() if found is not None and found.text else ""


def _pretty_name(url):
    try:
        name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
    except ValueError:
        name = url.rsplit("/", 1)[-1]
    name = re.sub(r"\.(?:mp3|m4a)$", "", name, flags=re.IGNORECASE)
    name = name.replace("_", " ").replace(".", " ")
    return re.sub(r"\s+", " ", name).strip(" -")


def track_title(url):
    """A short display suffix: ``Side A``, ``Sample``, or the filename."""
    name = _pretty_name(url)
    if SAMPLE_RE.search(name):
        digits = re.search(r"sample\s*(\d+)", name, re.IGNORECASE)
        return "Sample %s" % digits.group(1) if digits else "Sample"
    side = SIDE_RE.search(name)
    if side:
        return "Side %s" % side.group(1).upper()
    # "Artist - Title - Side A" keeps the useful tail if the name is long.
    if " - " in name and len(name) > 70:
        name = name.rsplit(" - ", 1)[-1]
    return name[:120]


def parse_feed(text):
    """Parse the WordPress RSS into posts with full-side and sample tracks."""
    if not isinstance(text, str) or not text.strip():
        return []
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError, TypeError):
        return []
    items = root.findall("./channel/item")
    posts = []
    for index, item in enumerate(items):
        title = clean_text(_text(item, "title"), 300)
        if not title:
            continue
        link = _text(item, "link")
        body = _text(item, CONTENT_NS + "encoded") or _text(item, "description")
        body = html.unescape(body) if isinstance(body, str) else ""
        urls = media_urls(body)
        tracks = []
        for url in urls:
            if url.lower().endswith(".zip"):
                continue
            lower = url.lower()
            if not (lower.endswith(".mp3") or lower.endswith(".m4a")):
                continue
            suffix = track_title(url)
            tracks.append({
                "url": url,
                "title": suffix,
                "sample": bool(SAMPLE_RE.search(urllib.parse.unquote(
                    urllib.parse.urlsplit(url).path))),
            })
        posts.append({
            "id": str(index + 1),
            "title": title,
            "url": link,
            "published": parse_date(_text(item, "pubDate")),
            "tracks": tracks,
            "zips": sorted(set(ZIP_RE.findall(body))),
            "content": body,
        })
    return posts


def _load_posts():
    body = http.fetch(FEED_URL, timeout=25, ttl=TTL)
    return parse_feed(body) if body else None


def posts():
    """All feed posts, parsed once from the cached RSS body (never raises)."""
    return cache.remember("demodulated:posts:v1", TTL, _load_posts,
                          sync=False) or []


def post(post_id):
    wanted = str(post_id or "").strip()
    if not wanted:
        return None
    return next((row for row in posts() if row.get("id") == wanted), None)


def summary(post_row):
    row = post_row if isinstance(post_row, dict) else {}
    tracks = row.get("tracks") or []
    sides = len([track for track in tracks if not track.get("sample")])
    samples = len(tracks) - sides
    pieces = [row.get("published") or ""]
    if sides:
        pieces.append("%d side%s" % (sides, "" if sides == 1 else "s"))
    if samples:
        pieces.append("%d sample%s" % (samples, "" if samples == 1 else "s"))
    if row.get("zips"):
        pieces.append("ZIP")
    return " · ".join(piece for piece in pieces if piece) or "Mixtape"
