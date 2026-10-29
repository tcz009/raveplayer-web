# -*- coding: utf-8 -*-
"""SoundCloud sets.

SoundCloud's API needs a registered client_id and their search page is
JS-only, so there is no legitimate way to search it from a server without an
app registration. What *does* work is the official widget: any public
track/set URL embeds in an iframe without credentials. So this is a curated
registry (resources/soundcloud.json) of verified live sets, played through
SoundCloud's own player — the same approach as the Mixcloud embeds.

Add more by appending {"title", "url"} entries; embed URLs are derived.
"""

import json
import os

from . import fetch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIDGET = "https://w.soundcloud.com/player/"
API = "https://api.soundcloud.com"
CLIENT_ID = os.environ.get("SOUNDCLOUD_CLIENT_ID", "").strip()


def configured():
    """True when a SoundCloud app client_id is present (search is possible)."""
    return bool(CLIENT_ID)


def search(query, limit=24):
    """Search public tracks via the official API. Requires SOUNDCLOUD_CLIENT_ID."""
    if not configured() or not query:
        return []
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 24
    url = "%s/tracks?q=%s&client_id=%s&limit=%d" % (
        API, fetch.quote(query), fetch.quote(CLIENT_ID), limit)
    data = fetch.fetch_json(url, timeout=25, ttl=900)
    if not isinstance(data, list):
        return []
    results = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        page = entry.get("permalink_url")
        if not isinstance(page, str) or not page.startswith("https://soundcloud.com/"):
            continue
        user = entry.get("user") or {}
        results.append({
            "title": str(entry.get("title") or "Untitled").strip(),
            "url": page,
            "user": str(user.get("username") or "").strip(),
            "embed": embed_url(page),
            "thumb": entry.get("artwork_url") or (user.get("avatar_url") or ""),
            "duration": entry.get("duration"),
            "plays": entry.get("playback_count"),
        })
    return results


def registry():
    path = os.path.join(ROOT, "soundcloud.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    entries = []
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        title = entry.get("title")
        if isinstance(url, str) and url.startswith("https://soundcloud.com/") and title:
            entries.append({"title": str(title)[:200], "url": url})
    return entries


def embed_url(page_url):
    """Official SoundCloud widget URL for a public track/set."""
    import urllib.parse
    return "%s?url=%s&color=%%23e8ff00&auto_play=false&hide_related=1&show_comments=0&visual=0" % (
        WIDGET, urllib.parse.quote(page_url, safe=""))
