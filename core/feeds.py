# -*- coding: utf-8 -*-
"""Podcast / mix feeds.

The most durable "internet-wide" source there is: an RSS feed only has to keep
existing, and the enclosures inside it are plain MP3s that Kodi can stream
directly. New feeds are added as data in resources/feeds.json.
"""

import json
import os
import re
import xml.etree.ElementTree as ET

from . import fetch as http

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.0}"
MEDIA = "{http://search.yahoo.com/mrss/}"
ATOM = "{http://www.w3.org/2005/Atom}"

FEED_TTL = 900


def registry():
    """Return the configured feeds, dropping malformed entries."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "feeds.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    feeds = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        feed_id = entry.get("id")
        url = entry.get("url")
        if not feed_id or not isinstance(url, str) or not url:
            continue
        feeds.append({
            "id": str(feed_id),
            "name": str(entry.get("name") or feed_id),
            "url": url,
            "plot": str(entry.get("plot") or ""),
        })
    return feeds


def feed(feed_id):
    return next((entry for entry in registry() if entry["id"] == feed_id), None)


def _elements(node, tag):
    """Find a child by its plain or Atom-namespaced name."""
    found = node.find(tag)
    if found is not None:
        return found
    return node.find(ATOM + tag)


def _text(node, *tags):
    for tag in tags:
        found = _elements(node, tag)
        if found is not None:
            value = " ".join(found.itertext()).strip()
            if value:
                return " ".join(value.split())
    return ""


def _links(item):
    return item.findall("link") + item.findall(ATOM + "link")


def _enclosure_url(item):
    enclosure = _elements(item, "enclosure")
    if enclosure is not None and enclosure.get("url"):
        return enclosure.get("url")
    content = item.find(MEDIA + "content")
    if content is not None and content.get("url"):
        return content.get("url")
    for link in _links(item):
        if not link.get("href"):
            continue
        rel = (link.get("rel") or "").lower()
        kind = (link.get("type") or "").lower()
        if rel == "enclosure" or kind.startswith("audio"):
            return link.get("href")
    return ""


def _duration(item):
    raw = _text(item, ITUNES + "duration")
    if not raw:
        return None
    if ":" in raw:
        # Ignore a fractional-seconds tail: "1:02:03.500" is still 1h 2m 3s.
        clock = raw.split(".", 1)[0]
        parts = [int(part) for part in re.findall(r"\d+", clock)][:3]
        if not parts:
            return None
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def parse(xml_text):
    """Parse an RSS/Atom document into episode dicts (no I/O)."""
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError, TypeError):
        return []
    items = root.findall("./channel/item")
    if not items:
        items = root.findall(".//" + ATOM + "entry")
    episodes = []
    for item in items:
        url = _enclosure_url(item)
        if not url:
            continue
        episodes.append({
            "title": _text(item, "title") or "Untitled",
            "url": url,
            "published": _text(item, "pubDate", ATOM + "updated"),
            "duration": _duration(item),
            "description": _text(item, "description", ITUNES + "summary", ATOM + "summary")[:400],
        })
    return episodes


def episodes(feed_id, limit=60):
    entry = feed(feed_id)
    if not entry:
        return []
    body = http.fetch(entry["url"], timeout=25, ttl=FEED_TTL)
    if not body:
        return []
    return parse(body)[:limit]
