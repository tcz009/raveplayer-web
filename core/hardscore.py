# -*- coding: utf-8 -*-
"""Hardscore.com — hardcore/jungle tape archive and event listings.

Two WordPress RSS feeds carry everything the section needs:

* the main feed has the mixes (direct MP3s live in <content:encoded>, and the
  <enclosure url> is the same file);
* the Events category feed has the event posts.

Items without a direct audio URL stay in the list and are opened on
Hardscore in a new tab (the SPA handles the ``external`` fallback).  All
requests are cached (30/60 min) and fail soft.
"""

from . import fetch, sources

BASE = "https://www.hardscore.com"
FEED = BASE + "/?feed=rss2"
EVENTS_FEED = BASE + "/?cat=3&feed=rss2"
FEED_TTL = 1800
EVENTS_TTL = 3600


def mixes(limit=30):
    """Latest Hardscore mixes; rows keep the direct MP3 when there is one."""
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 30
    try:
        episodes = sources.parse_rss(fetch.fetch(FEED, timeout=25, ttl=FEED_TTL), limit=limit)
    except Exception:  # noqa: BLE001 - section stays usable when the site is down
        return []
    rows = []
    for episode in episodes:
        title = episode.get("title") or ""
        if not title:
            continue
        media = episode.get("media") or []
        rows.append({
            "title": title,
            "date": episode.get("date") or "",
            "link": episode.get("link") or "",
            "media": media[0] if media else "",
        })
    return rows


def events(limit=10):
    """Hardscore event posts (they link out; no audio expected)."""
    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = 10
    try:
        episodes = sources.parse_rss(fetch.fetch(EVENTS_FEED, timeout=25, ttl=EVENTS_TTL), limit=limit)
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for episode in episodes:
        title = episode.get("title") or ""
        if title:
            rows.append({"title": title, "date": episode.get("date") or "",
                         "link": episode.get("link") or "", "media": ""})
    return rows
