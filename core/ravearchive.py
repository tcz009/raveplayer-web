# -*- coding: utf-8 -*-
"""Rave Archive UK — rave tape-pack scans with a Mixcloud player on each post.

The WordPress RSS feed lists the releases (year and DJ names are categories);
the post page embeds the set with Mixcloud.  The list links to a small detail
endpoint that resolves the post page to an embeddable Mixcloud page, so the
set plays in the existing (CSP-allowed) Mixcloud widget without leaving the
app — and falls back to the source page when no embed is found.
"""

import re
import urllib.parse

from . import fetch, sources

BASE = "https://rave-archive.com"
FEED = BASE + "/feed/"
FEED_TTL = 3600
PAGE_TTL = 6 * 3600

_YEAR_RE = re.compile(r"^(19[5-9]\d|20[0-2]\d)$")


def _slug(link):
    path = urllib.parse.urlsplit(str(link or "")).path.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else ""


def releases(limit=30):
    """Latest Rave Archive releases with year and DJ categories."""
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 30
    try:
        episodes = sources.parse_rss(fetch.fetch(FEED, timeout=25, ttl=FEED_TTL), limit=limit)
    except Exception:  # noqa: BLE001 - fail-soft section
        return []
    rows = []
    for episode in episodes:
        link = episode.get("link") or ""
        title = episode.get("title") or ""
        if not link or not title:
            continue
        year = ""
        djs = []
        for category in episode.get("categories") or []:
            if not year and _YEAR_RE.match(category):
                year = category
            elif category:
                djs.append(category)
        rows.append({
            "title": title,
            "slug": _slug(link),
            "link": link,
            "date": episode.get("date") or "",
            "year": year,
            "djs": djs,
        })
    return rows


def release(slug):
    """Resolve one post page to its Mixcloud page; None when unavailable."""
    wanted = urllib.parse.unquote(str(slug or "")).strip("/")
    if not wanted:
        return None
    try:
        entry = next((item for item in releases(limit=50) if item["slug"] == wanted), None)
        if not entry:
            return None
        body = fetch.fetch(entry["link"], timeout=25, ttl=PAGE_TTL)
        if not body:
            return None
        cloudcast = sources.mixcloud_page(body)
        return {
            "title": entry["title"],
            "slug": wanted,
            "link": entry["link"],
            "mixcloud": cloudcast,
        }
    except Exception:  # noqa: BLE001 - fail-soft on a third-party page
        return None
