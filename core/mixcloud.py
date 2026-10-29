# -*- coding: utf-8 -*-
"""Mixcloud lookup.

Mixcloud is where most present-day UK garage sets live — DJ EZ's NUVOLVE
radio series, Garage Nation recordings, fabric mixes. Their public search API
needs no key, and their official iframe widget is the sanctioned way to play a
cloudcast, so the web app embeds the widget rather than trying to extract
audio streams (which are HLS and intentionally not public).
"""

import urllib.parse

from . import fetch

API = "https://api.mixcloud.com"
WIDGET = "https://www.mixcloud.com/widget/iframe/"


def embed_url(page_url):
    """Official Mixcloud widget URL for a cloudcast page."""
    return "%s?hide_cover=1&mini=1&light=0&feed=%s" % (WIDGET, urllib.parse.quote(page_url, safe=""))


def search(query, limit=24):
    """Search cloudcasts; returns dicts with title/url/user/embed/thumb."""
    try:
        limit = max(1, min(int(limit), 48))
    except (TypeError, ValueError):
        limit = 24
    url = "%s/search/?q=%s&type=cloudcast&limit=%d" % (API, fetch.quote(query), limit)
    data = fetch.fetch_json(url, timeout=25, ttl=900)
    if not isinstance(data, dict):
        return []
    results = []
    for entry in data.get("data", []):
        if not isinstance(entry, dict):
            continue
        page = entry.get("url")
        if not isinstance(page, str) or not page.startswith("https://www.mixcloud.com/"):
            continue
        user = entry.get("user") or {}
        pictures = entry.get("pictures") or {}
        results.append({
            "title": str(entry.get("name") or "Untitled").strip(),
            "url": page,
            "user": str(user.get("name") or "").strip(),
            "embed": embed_url(page),
            "thumb": pictures.get("medium") or pictures.get("thumbnail") or "",
            "created": entry.get("created_time") or "",
            "duration": entry.get("audio_length"),
        })
    return results
