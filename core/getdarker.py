# -*- coding: utf-8 -*-
"""GetDarker — dubstep/grime/140 sets (Mixcloud account ``getdarker`` + RSS).

The site's RSS feed is the richer source: every set post embeds its Mixcloud
cloudcast, so the section gets a real embed (and a link to the source post)
without scraping.  If the feed ever loses those embeds, fall back to the
public Mixcloud API for the curated ``getdarker`` account.
"""

from . import fetch, mixcloud, sources

BASE = "https://getdarker.com"
FEED = BASE + "/feed/"
FEED_TTL = 1800
ACCOUNT = "getdarker"


def _row(episode):
    title = episode.get("title") or ""
    if not title:
        return None
    cloudcast = episode.get("mixcloud") or ""
    return {
        "title": title,
        "date": episode.get("date") or "",
        "link": episode.get("link") or "",
        "mixcloud": cloudcast,
        "media": (episode.get("media") or [""])[0],
    }


def sets(limit=30):
    """GetDarker sets from the RSS feed, with the Mixcloud API as fallback."""
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 30
    rows = []
    try:
        episodes = sources.parse_rss(fetch.fetch(FEED, timeout=25, ttl=FEED_TTL), limit=limit)
        rows = [row for row in (_row(episode) for episode in episodes) if row]
    except Exception:  # noqa: BLE001 - fall through to the Mixcloud account
        rows = []
    if not any(row.get("mixcloud") for row in rows):
        try:
            rows = [{
                "title": entry.get("title") or "",
                "date": str(entry.get("created") or "")[:10],
                "link": entry.get("url") or "",
                "mixcloud": entry.get("url") or "",
                "media": "",
            } for entry in mixcloud.account(ACCOUNT, limit=limit)]
        except Exception:  # noqa: BLE001 - both sources down: empty section
            rows = []
    return [row for row in rows if row.get("title")]
