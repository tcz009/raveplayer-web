# -*- coding: utf-8 -*-
"""Podcast / mix feeds.

The most durable "internet-wide" source there is: an RSS feed only has to keep
existing, and the enclosures inside it are plain MP3s that Kodi can stream
directly. New feeds are added as data in resources/feeds.json.
"""

import html
import json
import math
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import cache, fetch as http, kodi

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.0}"
MEDIA = "{http://search.yahoo.com/mrss/}"
ATOM = "{http://www.w3.org/2005/Atom}"

FEED_TTL = 900
# DejaVu FM's Podomatic feed is ~10 MB and 4,000+ episodes: cold it takes
# 11-20 s on the NAS, and re-parsing it on a request is the expensive part.
# A feed marked ``"warm": true`` in feeds.json is cached for a day and follows
# the same stale-while-revalidate policy as the 20bensons homepage cache in
# core/bensons.py: once the parsed list reaches 70% of its TTL one background
# thread refreshes it, an expired entry is still served when upstream fails,
# and the first request after a restart starts the warm off the request path.
WARM_TTL = 24 * 60 * 60
WARM_REVALIDATE_AFTER = int(WARM_TTL * 0.7)
# Stay away from a failing upstream for a while: the stale list is still being
# served, and an offline NAS must not turn every room load into another
# 25-second feed download.
WARM_RETRY_SECONDS = 5 * 60

# /api/podcasts only needs episode counts.  A cold cache used to fetch every
# feed one after another (5 x 25 s) on the request thread; counts are
# independent, so run them in a small bounded pool with a shorter per-feed
# timeout.  Worst case is now ceil(feeds / COUNT_WORKERS) x COUNT_TIMEOUT.
COUNT_WORKERS = 4
COUNT_TIMEOUT = 12

# Display-only, third-party: same bounds as core/archive.py.
MAX_DURATION_SECONDS = 24 * 60 * 60
MAX_DURATION_PART_DIGITS = 6


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
        try:
            max_bytes = int(entry.get("max_bytes") or 0)
        except (TypeError, ValueError):
            max_bytes = 0
        feeds.append({
            "id": str(feed_id),
            "name": str(entry.get("name") or feed_id),
            "url": url,
            "plot": str(entry.get("plot") or ""),
            "max_bytes": max_bytes if max_bytes > 0 else 0,
            "cache_body": entry.get("cache_body", True) is not False,
            "warm": entry.get("warm") is True,
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


def _decode(value):
    """Decode HTML entities, including titles a CMS encoded twice.

    Scrapers see ``&amp;`` in a text node and ``&amp;#x2013;`` when the source
    encoded the entity itself.  Loop until stable (bounded) so the rendered
    title carries ``&`` and ``–``, never the entity source.
    """
    text = str(value or "")
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text


def _text(node, *tags):
    for tag in tags:
        found = _elements(node, tag)
        if found is not None:
            value = " ".join(found.itertext()).strip()
            if value:
                return " ".join(_decode(value).split())
    return ""


def _links(item):
    return item.findall("link") + item.findall(ATOM + "link")


def _media_url(value):
    """Return an http(s) URL only (mirrors the add-on's ``_media_url``)."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    lowered = value.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        return ""
    return value


def _enclosure_url(item):
    enclosure = _elements(item, "enclosure")
    if enclosure is not None:
        url = _media_url(enclosure.get("url"))
        if url:
            return url
    content = item.find(MEDIA + "content")
    if content is not None:
        url = _media_url(content.get("url"))
        if url:
            return url
    for link in _links(item):
        if not link.get("href"):
            continue
        rel = (link.get("rel") or "").lower()
        kind = (link.get("type") or "").lower()
        if rel == "enclosure" or kind.startswith("audio"):
            url = _media_url(link.get("href"))
            if url:
                return url
    return ""


def _bounded_duration(seconds):
    """Return a sane positive duration, or None (never a huge int)."""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError, OverflowError):
        return None
    return seconds if 0 < seconds <= MAX_DURATION_SECONDS else None


def _duration(item):
    raw = _text(item, ITUNES + "duration")
    if not raw:
        return None
    if ":" in raw:
        # Ignore a fractional-seconds tail: "1:02:03.500" is still 1h 2m 3s.
        clock = raw.split(".", 1)[0]
        raw_parts = re.findall(r"\d+", clock)[:3]
        if not raw_parts or any(len(part) > MAX_DURATION_PART_DIGITS for part in raw_parts):
            return None
        seconds = 0
        for part in raw_parts:
            seconds = seconds * 60 + int(part)
        return _bounded_duration(seconds)
    try:
        # Third-party feeds: "inf"/"1e400"/"nan" must not raise OverflowError
        # (mirrors the Kodi add-on's duration parsing fix).
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds):
        return None
    return _bounded_duration(seconds)


_ENTITY_RE = re.compile(r"<!ENTITY", re.IGNORECASE)


def parse(xml_text):
    """Parse an RSS/Atom document into episode dicts (no I/O).

    Entity declarations are refused before parsing: ElementTree expands
    internal entities, so a hostile feed could otherwise do a billion-laughs
    memory bomb on the request thread (mirrors the add-on's K5 fix).
    """
    if not isinstance(xml_text, str):
        return []
    if _ENTITY_RE.search(xml_text):
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


def _feed_body(entry, timeout):
    """Fetch one feed with its optional large-body allowance.

    DejaVu FM's Podomatic feed is ~10 MB (4,000+ episodes); the default 8 MiB
    fetch cap would reject it.  ``max_bytes`` is an explicit per-feed opt-in,
    and ``cache_body: false`` keeps an oversized body out of the 8 MiB disk
    cache (the parsed episode list is cached instead).
    """
    return http.fetch(
        entry["url"], timeout=timeout,
        ttl=FEED_TTL if entry.get("cache_body", True) else 0,
        max_bytes=entry.get("max_bytes") or None)


def _load_episodes(entry):
    body = _feed_body(entry, 25)
    return parse(body) if body else None


class _WarmState(object):
    """Process-local scheduling state for one large feed.

    A cold request and a background refresh share ``load_lock`` so only one
    upstream download can run at a time; ``refresh_lock`` only guards the small
    state transition, so a request never waits for a refresh to finish.
    """

    __slots__ = ("load_lock", "refresh_lock", "refresh_active",
                 "last_attempt", "done")

    def __init__(self):
        self.load_lock = threading.Lock()
        self.refresh_lock = threading.Lock()
        self.refresh_active = False
        self.last_attempt = 0.0
        self.done = threading.Event()
        self.done.set()


# No Flask/app context is touched here: the request hook and the daemon
# workers run freely outside a request, exactly like core/bensons.py.
_WARM_STATES_LOCK = threading.Lock()
_WARM_STATES = {}
_WARM_LOCK = threading.Lock()
_WARMED = False


def _episode_key(entry):
    return "feeds:episodes:v1:%s" % entry["url"]


def _state_for(key):
    with _WARM_STATES_LOCK:
        state = _WARM_STATES.get(key)
        if state is None:
            state = _WarmState()
            _WARM_STATES[key] = state
        return state


def _publish_episodes(entry, rows):
    """Replace the parsed cache only with a usable, non-empty list."""
    if not isinstance(rows, list) or not rows:
        return False
    cache.put(_episode_key(entry), rows, sync=False)
    return True


def _refresh_worker(entry, state):
    """One upstream revalidation; never raises and never touches a request."""
    started = time.time()
    try:
        with state.load_lock:
            # Another thread/process may have refreshed while this worker was
            # queued; in that case there is nothing left to do.
            age = cache.age(_episode_key(entry))
            if age is not None and age < WARM_REVALIDATE_AFTER:
                return
            rows = _load_episodes(entry)
            if _publish_episodes(entry, rows):
                kodi.log("feed %s: refreshed %d episodes in %.2fs"
                         % (entry["id"], len(rows), time.time() - started),
                         level=10)
            else:
                # fetch() has already logged a transport/http warning.  Keep
                # the previous list and stay quiet until the retry cooldown.
                kodi.log("feed %s: refresh had no episodes; keeping stale cache"
                         % entry["id"], level=20)
    except Exception:  # noqa: BLE001 - a background refresh must never raise
        kodi.log("feed %s: refresh failed; keeping stale cache" % entry["id"],
                 level=10)
    finally:
        with state.refresh_lock:
            state.refresh_active = False
            state.done.set()


def _schedule_refresh(entry):
    """Start the single background refresh, or return False if one is due later.

    Deduped two ways: an active worker suppresses every other request, and a
    short cooldown after an attempt keeps an offline/failing upstream from
    being retried on every page load.  The caller never joins the thread.
    """
    state = _state_for(_episode_key(entry))
    now = time.time()
    with state.refresh_lock:
        if state.refresh_active:
            return False
        if state.last_attempt and now - state.last_attempt < WARM_RETRY_SECONDS:
            return False
        state.refresh_active = True
        state.last_attempt = now
        state.done.clear()
    try:
        thread = threading.Thread(target=_refresh_worker, args=(entry, state),
                                  name="feed-warm-%s" % entry["id"], daemon=True)
        thread.start()
    except Exception:  # pragma: no cover - thread creation unavailable
        with state.refresh_lock:
            state.refresh_active = False
        state.done.set()
        return False
    return True


def _maybe_refresh(entry):
    """Schedule a refresh once the cached list is old enough; never waits."""
    age = cache.age(_episode_key(entry))
    if age is not None and age >= WARM_REVALIDATE_AFTER:
        return _schedule_refresh(entry)
    return False


def warm():
    """Warm every large feed in the background, once per process.

    Called from a request hook so a deploy/restart does not leave DejaVu cold
    (its 10 MB feed is an 11-20 s cold request on the NAS).  It only inspects a
    timestamp and (at most) starts one daemon thread per feed, so the request
    itself is never delayed and an offline upstream produces one failed attempt
    rather than a per-request retry loop.  The flag is consumed even when the
    cache is already fresh: there is nothing for a second call in this process
    to do.
    """
    global _WARMED
    with _WARM_LOCK:
        if _WARMED:
            return False
        _WARMED = True
    started = False
    for entry in registry():
        if not entry.get("warm"):
            continue
        age = cache.age(_episode_key(entry))
        if age is None or age >= WARM_REVALIDATE_AFTER:
            started = _schedule_refresh(entry) or started
    return started


def _reset_state():
    """Reset process-local scheduling state (tests and local benchmarks only)."""
    global _WARMED
    with _WARM_LOCK:
        _WARMED = False
    with _WARM_STATES_LOCK:
        states = list(_WARM_STATES.values())
        _WARM_STATES.clear()
    for state in states:
        with state.refresh_lock:
            state.refresh_active = False
            state.last_attempt = 0.0
            state.done.set()


def _warm_episodes(entry):
    """Parsed list for a large feed, served stale while one refresh runs.

    The cached list is returned immediately whenever it exists, however old it
    is.  A list past :data:`WARM_REVALIDATE_AFTER` additionally queues one
    background refresh; a truly cold cache loads synchronously, single-flighted
    with any warm already in progress.  A failed load leaves a previous list
    untouched and, on a first-ever failure, returns an empty list.
    """
    key = _episode_key(entry)
    rows = cache.get(key, None, copy_value=False)
    if rows is not cache.MISSING:
        _maybe_refresh(entry)
        return rows

    # Only a cold miss (or a queued refresh) needs process-local state.
    state = _state_for(key)
    with state.load_lock:
        # A warm or another cold request may have filled the cache while we
        # waited for the load lock.
        rows = cache.get(key, None, copy_value=False)
        if rows is not cache.MISSING:
            _maybe_refresh(entry)
            return rows
        rows = _load_episodes(entry)
        if _publish_episodes(entry, rows):
            return rows
        stale = cache.get(key, None, copy_value=False)
        return [] if stale is cache.MISSING else stale


def episodes(feed_id, limit=None):
    entry = feed(feed_id)
    if not entry:
        return []
    if entry.get("warm"):
        rows = _warm_episodes(entry)
    else:
        key = "feeds:episodes:v1:%s" % entry["url"]
        # Read the parsed list without the cache's defensive deep copy: DejaVu
        # FM has 4,000+ episodes and copying that list on every /api/latest
        # cost more than the rest of the merge.  The list is only sliced/
        # iterated below and by the two routes that call this; nothing mutates
        # it.  A cold miss still builds a private list and ``put`` deep-copies
        # it into the memory layer.
        rows = cache.get(key, FEED_TTL, copy_value=False)
        if rows is cache.MISSING:
            rows = _load_episodes(entry)
            if isinstance(rows, list):
                cache.put(key, rows, sync=False)
    if not isinstance(rows, list):
        return []
    try:
        cap = None if limit is None else max(1, int(limit))
    except (TypeError, ValueError):
        cap = None
    return rows if cap is None else rows[:cap]


def _count_from_body(entry):
    """Parse one feed and return its episode count, or None on a fetch miss."""
    body = _feed_body(entry, COUNT_TIMEOUT)
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
