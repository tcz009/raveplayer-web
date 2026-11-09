# -*- coding: utf-8 -*-
"""Mikus Musik — a Blogger blog of house/garage/jungle mixes.

Blogger's JSON feed (``feeds/posts/default?alt=json&max-results=150``) returns
the full post HTML in one request, so the room can list posts and a detail
route can expose the links without another scrape.  Posts usually carry a
Mixcloud widget, often a SoundCloud or YouTube link, and one or more Google
Drive files.  Mixcloud/SoundCloud rows open the existing inline embed dock;
YouTube and anything else are hand-offs; Drive files resolve to a direct audio
URL through :mod:`core.drive`.
"""

import html
import json
import re
import urllib.parse

from . import cache
from . import fetch as http
from . import mixcloud as mixcloud_module
from .sources import media_urls, mixcloud_page

FEED_URL = ("http://mikusmusik.blogspot.com/feeds/posts/default"
            "?alt=json&max-results=150")
SOURCE_URL = "http://mikusmusik.blogspot.com/"
SOURCE_LABEL = "Mikus Musik"
TTL = 6 * 60 * 60

ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
HREF_RE = re.compile(r"\bhref\s*=\s*([\"'])(.*?)\1", re.IGNORECASE | re.DOTALL)
SOUNDCLOUD_RE = re.compile(
    r"https?://(?:www\.)?soundcloud\.com/[A-Za-z0-9_@%+./-]+", re.IGNORECASE)
YOUTUBE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com/(?:watch\?[^\"'<>\s]*v=|embed/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{6,20})", re.IGNORECASE)
DRIVE_RE = re.compile(
    r"https?://(?:drive|docs)\.google\.com/"
    r"(?:file/d/|open\?id=|uc\?(?:export=download&)?id=)([A-Za-z0-9_-]{10,100})",
    re.IGNORECASE)
TITLE_RE = re.compile(r"<[^>]+>")


def _first(value):
    if isinstance(value, dict):
        text = value.get("$t")
        return text if isinstance(text, str) else ""
    return value if isinstance(value, str) else ""


def _clean(value, limit=300):
    text = html.unescape(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _href(attrs):
    match = HREF_RE.search(attrs or "")
    return html.unescape(match.group(2)).strip() if match else ""


def _post_id(entry):
    identifier = _first(entry.get("id")) if isinstance(entry, dict) else ""
    match = re.search(r"post-(\d{6,})", identifier)
    if match:
        return match.group(1)
    link = _alternate_link(entry)
    match = re.search(r"/(\d{4})/(\d{2})/([^/?#]+)\.html", link)
    if match:
        return "%s%s%s" % (match.group(1), match.group(2), match.group(3))
    return ""


def _alternate_link(entry):
    links = entry.get("link") if isinstance(entry, dict) else []
    fallback = ""
    for link in links if isinstance(links, list) else []:
        if not isinstance(link, dict):
            continue
        href = link.get("href")
        if not isinstance(href, str) or not href:
            continue
        if (link.get("rel") or "").lower() == "alternate":
            return href
        if not fallback and "blogspot.com" in href:
            fallback = href
    return fallback


def _soundcloud_urls(text):
    found = []
    for match in SOUNDCLOUD_RE.finditer(text or ""):
        value = html.unescape(match.group(0)).rstrip(").,;'\"")
        try:
            parts = urllib.parse.urlsplit(value)
        except ValueError:
            continue
        path = parts.path.strip("/")
        if not path or path.startswith("widget"):
            continue
        last = path.rsplit("/", 1)[-1].lower()
        if last in ("tracks", "sets", "albums", "likes", "reposts", "followers"):
            continue
        if value not in found:
            found.append(value)
    return found


def _youtube_urls(text):
    found = []
    for match in YOUTUBE_RE.finditer(text or ""):
        identifier = match.group(1)
        url = "https://www.youtube.com/watch?v=%s" % urllib.parse.quote(identifier, safe="")
        if url not in found:
            found.append(url)
    return found


def _drive_files(text):
    files = []
    seen = set()
    for anchor in ANCHOR_RE.finditer(text or ""):
        href = _href(anchor.group(1))
        match = DRIVE_RE.match(href)
        if not match:
            continue
        identifier = match.group(1)
        if identifier in seen:
            continue
        seen.add(identifier)
        title = TITLE_RE.sub(" ", anchor.group(2) or "")
        title = _clean(title, 160)
        files.append({
            "id": identifier,
            "title": title,
            "url": "https://drive.google.com/file/d/%s/view" % identifier,
        })
    # A bare URL (not wrapped in an anchor) is still a valid file.
    for match in DRIVE_RE.finditer(text or ""):
        identifier = match.group(1)
        if identifier in seen:
            continue
        seen.add(identifier)
        files.append({
            "id": identifier,
            "title": "",
            "url": "https://drive.google.com/file/d/%s/view" % identifier,
        })
    return files


def parse_feed(data):
    """Turn a Blogger JSON document into post dicts; malformed input -> []."""
    if not isinstance(data, dict):
        return []
    feed = data.get("feed")
    if not isinstance(feed, dict):
        return []
    posts = []
    for entry in feed.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        title = _clean(_first(entry.get("title")), 300)
        if not title:
            continue
        post_id = _post_id(entry)
        if not post_id:
            continue
        content = _first(entry.get("content")) or _first(entry.get("summary"))
        content = html.unescape(content) if isinstance(content, str) else ""
        published = _first(entry.get("published")) or _first(entry.get("updated"))
        files = _drive_files(content)
        posts.append({
            "id": post_id,
            "title": title,
            "url": _alternate_link(entry),
            "published": published[:10],
            "content": content,
            "mixcloud": mixcloud_page(content),
            "soundcloud": _soundcloud_urls(content),
            "youtube": _youtube_urls(content),
            "drive": files,
            "media": media_urls(content),
        })
    return posts


def _load_posts():
    data = http.fetch_json(FEED_URL, timeout=30, ttl=TTL)
    return parse_feed(data) if isinstance(data, dict) else None


def posts():
    """All posts from the cached Blogger JSON feed (never raises)."""
    return cache.remember("mikus:posts:v1", TTL, _load_posts,
                          sync=False) or []


def post(post_id):
    wanted = str(post_id or "").strip()
    if not wanted:
        return None
    return next((row for row in posts() if row.get("id") == wanted), None)


def summary(post_row):
    """A compact folder subtitle: date plus the media it links to."""
    row = post_row if isinstance(post_row, dict) else {}
    kinds = []
    if row.get("mixcloud"):
        kinds.append("Mixcloud")
    if row.get("soundcloud"):
        kinds.append("SoundCloud")
    if row.get("drive"):
        kinds.append("%d Drive" % len(row["drive"]))
    if row.get("youtube"):
        kinds.append("YouTube")
    pieces = [row.get("published") or "", " · ".join(kinds) or "Post"]
    return " · ".join(piece for piece in pieces if piece)


def soundcloud_embed(url):
    value = str(url or "").strip()
    if not value:
        return ""
    return ("https://w.soundcloud.com/player/?url=%s"
            "&color=%%23e8ff00&auto_play=false&hide_related=1"
            "&show_comments=0&visual=0" % urllib.parse.quote(value, safe=""))


def mixcloud_embed(url):
    value = str(url or "").strip()
    if not value:
        return ""
    try:
        return mixcloud_module.embed_url(value)
    except Exception:  # noqa: BLE001 - a malformed page is not a fatal detail
        return ""
