# -*- coding: utf-8 -*-
"""Podcast / mix feeds.

The most durable "internet-wide" source there is: an RSS feed only has to keep
existing, and the enclosures inside it are plain MP3s that Kodi can stream
directly. New feeds are added as data in resources/feeds.json.
"""

import json
import math
import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import cache, fetch as http, kodi

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.0}"
MEDIA = "{http://search.yahoo.com/mrss/}"
ATOM = "{http://www.w3.org/2005/Atom}"

FEED_TTL = 900
# /api/podcasts only needs episode counts.  A cold cache used to fetch every
# feed one after another (5 x 25 s) on the request thread; counts are
# independent, so run them in a small bounded pool with a shorter per-feed
# timeout.  Worst case is now ceil(feeds / COUNT_WORKERS) x COUNT_TIMEOUT.
COUNT_WORKERS = 4
COUNT_TIMEOUT = 12


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
        # Third-party feeds: "inf"/"1e400"/"nan" must not raise OverflowError
        # (mirrors the Kodi add-on's duration parsing fix).
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds):
        return None
    try:
        return int(seconds)
    except (OverflowError, ValueError):
        return None


_ENTITY_RE = re.compile(r"<!ENTITY", re.IGNORECASE)


def parse(xml_text):
    """Parse an RSS/Atom document into episode dicts (no I/O).

    Entity declarations are refused before parsing: ElementTree expands
    internal entities, so a hostile feed could otherwise do a billion-laughs
    memory bomb on the request thread (mirrors the add-on's K5 fix).
    """
    if not isinstance(xml_text, str):
        return []
    if _ENTITY_RE.search(xml_text[:65536]):
        kodi.log("refusing feed with entity declarations", level=30)
        return []
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


def _count_from_body(entry):
    """Parse one feed and return its episode count, or None on a fetch miss."""
    body = http.fetch(entry["url"], timeout=COUNT_TIMEOUT, ttl=FEED_TTL)
    if not body:
        return None
    return len(parse(body))


def count(feed_id):
    """Episode count for a feed, cached for :data:`FEED_TTL`.

    ``/api/podcasts`` only needs the number for its folder rows, and fetching
    and parsing the full 200-episode document for every feed on every page
    load is wasted work.  The count shares the feed body cache TTL, so it is
    recomputed at most once per TTL window.  A failed fetch returns 0 and is
    not cached, so a temporary outage recovers on the next request.
    """
    entry = feed(feed_id)
    if not entry:
        return 0
    cached = cache.remember(
        "feeds:count:%s" % entry["url"], FEED_TTL, lambda: _count_from_body(entry)
    )
    return cached if isinstance(cached, int) and cached >= 0 else 0

def counts(feed_ids):
    """Return ``{feed_id: count}``, fetching independent feeds in parallel.

    Kept separate from :func:`count` so ``/api/podcasts`` can bound its cold
    latency: the feeds are independent, and a dead feed contributes 0 instead
    of adding its timeout to the next one.  Falls back to sequential counts if
    the worker threads cannot be started.
    """
    unique = []
    for feed_id in feed_ids or []:
        if isinstance(feed_id, str) and feed_id not in unique:
            unique.append(feed_id)
    if not unique:
        return {}
    results = {}
    try:
        with ThreadPoolExecutor(max_workers=min(COUNT_WORKERS, len(unique))) as pool:
            futures = {pool.submit(count, feed_id): feed_id for feed_id in unique}
            for future in as_completed(futures):
                feed_id = futures[future]
                try:
                    value = future.result()
                except Exception as exc:  # one broken feed must not hide the rest
                    kodi.log("feed count failed for %s (%s)" % (feed_id, exc), level=30)
                    value = 0
                results[feed_id] = value if isinstance(value, int) and value >= 0 else 0
    except RuntimeError:  # pragma: no cover - thread pool unavailable
        return {feed_id: count(feed_id) for feed_id in unique}
    for feed_id in unique:
        results.setdefault(feed_id, 0)
    return results
